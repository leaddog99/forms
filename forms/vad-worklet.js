/* vad-worklet.js — energy VAD on the audio-rendering thread (AudioWorklet).
 *
 * Ports the cook.html ScriptProcessor VAD off the MAIN thread onto the dedicated
 * audio thread, so main-thread/GC jank can't stall capture (the iPad hands-free
 * regression: 3s+ delays, lost "next"s). All buffers are PRE-ALLOCATED — no
 * per-quantum allocation in the audio callback (the exact mistake that tanked the
 * old main-thread pre-roll). Only EVENTS are posted to the main thread
 * (start / end / drop / heartbeat / barge); per-frame work stays here.
 *
 * Behavior is a faithful port of onAudioFrame: adaptive noise floor, awaiting-Q
 * thresholds, adaptive silence-hang endpointing, MIN_VOICE drop gate, the speaking
 * guard (Chef's own voice ignored), and a now-safe onset pre-roll ring.
 *
 * Protocol — main → worklet (port.postMessage):
 *   {cmd:'config', cfg:{...}}   replace tunables + re-init buffers
 *   {cmd:'active', on}          start/stop processing (resets state on start)
 *   {cmd:'speaking', on}        Chef speaking? (true = ignore input; false starts the echo guard)
 *   {cmd:'awaiting', on}        awaiting a question after the wake word (eager thresholds, full hang)
 * worklet → main:
 *   {type:'start'}                          speech onset
 *   {type:'end', samples:Float32Array, hangMs, capMs}   utterance (samples transferred)
 *   {type:'drop', voiceMs, capMs}           too-short capture, ignored
 *   {type:'hb', peak, floor, onT, capturing}  ~4s heartbeat for diagnostics
 *   {type:'barge'}                          loud voice while Chef speaks (barge-in)
 */
class VadProcessor extends AudioWorkletProcessor {
  constructor(opts) {
    super();
    const o = (opts && opts.processorOptions) || {};
    this.rate = sampleRate;            // AudioWorkletGlobalScope global
    this.FRAME = 4096;                  // analysis frame — matches the old ScriptProcessor frame
    this.frameMs = this.FRAME / this.rate * 1000;
    this.cfg = {
      hangFull: 800, hangShort: 420, shortCmdMax: 360, minVoice: 170,
      guardMs: 700, bargeIn: false, bargeThresh: 0.06, preRollMs: 250, maxMs: 15000,
    };
    if (o.cfg) Object.assign(this.cfg, o.cfg);
    this.active = false; this.speaking = false; this.awaiting = false;
    this._initBuffers();
    this.reset();
    this.port.onmessage = (e) => this._onMsg(e.data);
  }

  _initBuffers() {
    this.acc = new Float32Array(this.FRAME); this.accPos = 0;
    this.maxSamples = Math.ceil(this.cfg.maxMs / 1000 * this.rate);
    this.cap = new Float32Array(this.maxSamples); this.capLen = 0;
    this.preFrames = Math.max(1, Math.round(this.cfg.preRollMs / this.frameMs));
    this.ring = [];
    for (let i = 0; i < this.preFrames; i++) this.ring.push(new Float32Array(this.FRAME));
    this.ringPos = 0; this.ringFilled = 0;
    this.hbEvery = Math.max(1, Math.round(4000 / this.frameMs));
  }

  reset() {
    this.capturing = false; this.voiceMs = 0; this.silenceMs = 0; this.capLen = 0;
    this.noiseFloor = 0.01; this.accPos = 0; this.ringPos = 0; this.ringFilled = 0;
    this.guardFramesLeft = 0; this.hbFrames = 0; this.hbPeak = 0;
  }

  _onMsg(d) {
    if (d.cmd === 'config') { Object.assign(this.cfg, d.cfg || {}); this._initBuffers(); }
    else if (d.cmd === 'active') { this.active = !!d.on; if (d.on) this.reset(); }
    else if (d.cmd === 'speaking') {
      this.speaking = !!d.on;
      if (d.on) { this.capturing = false; this.capLen = 0; this.voiceMs = 0; this.silenceMs = 0; }
      else { this.guardFramesLeft = Math.round(this.cfg.guardMs / this.frameMs); }
    }
    else if (d.cmd === 'awaiting') { this.awaiting = !!d.on; }
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!this.active || !ch) return true;
    let i = 0;
    while (i < ch.length) {
      const take = Math.min(this.FRAME - this.accPos, ch.length - i);
      this.acc.set(ch.subarray(i, i + take), this.accPos);
      this.accPos += take; i += take;
      if (this.accPos >= this.FRAME) { this._frame(this.acc); this.accPos = 0; }
    }
    return true;
  }

  _appendCap(frame) {
    const take = Math.min(this.cap.length - this.capLen, frame.length);
    if (take > 0) { this.cap.set(frame.subarray(0, take), this.capLen); this.capLen += take; }
  }

  _frame(buf) {
    let sum = 0;
    for (let k = 0; k < buf.length; k++) { const s = buf[k]; sum += s * s; }
    const rms = Math.sqrt(sum / buf.length);

    // ~4s heartbeat (before the speaking guard, so a stuck-speaking state is visible)
    this.hbPeak = rms > this.hbPeak ? rms : this.hbPeak;
    if (++this.hbFrames >= this.hbEvery) {
      const onTh = Math.max(0.022, this.noiseFloor * 3.5);
      this.port.postMessage({ type: 'hb', peak: this.hbPeak, floor: this.noiseFloor, onT: onTh, capturing: this.capturing });
      this.hbFrames = 0; this.hbPeak = 0;
    }

    if (this.speaking) { if (this.cfg.bargeIn && rms > this.cfg.bargeThresh) this.port.postMessage({ type: 'barge' }); return; }
    if (this.guardFramesLeft > 0) { this.guardFramesLeft--; return; }  // echo tail after Chef stops

    const onT = this.awaiting ? Math.max(0.014, this.noiseFloor * 2.5) : Math.max(0.022, this.noiseFloor * 3.5);
    const offT = this.awaiting ? Math.max(0.010, this.noiseFloor * 1.8) : Math.max(0.015, this.noiseFloor * 2.2);

    if (!this.capturing) {
      this.noiseFloor = 0.95 * this.noiseFloor + 0.05 * rms;        // track ambient while quiet
      if (rms > onT) {
        this.capturing = true; this.voiceMs = 0; this.silenceMs = 0; this.capLen = 0;
        // seed the pre-roll (oldest→newest) so the word's onset isn't clipped — safe
        // now: the ring is pre-allocated, copy-only, no allocation in the callback.
        const N = Math.min(this.ringFilled, this.preFrames);
        for (let f = 0; f < N; f++) {
          const idx = (this.ringPos - N + f + this.preFrames) % this.preFrames;
          this._appendCap(this.ring[idx]);
        }
        this.port.postMessage({ type: 'start' });
      } else {
        this.ring[this.ringPos].set(buf);                           // rolling onset buffer (no alloc)
        this.ringPos = (this.ringPos + 1) % this.preFrames;
        if (this.ringFilled < this.preFrames) this.ringFilled++;
      }
    }

    if (this.capturing) {
      this._appendCap(buf);
      if (rms > offT) { this.voiceMs += this.frameMs; this.silenceMs = 0; } else { this.silenceMs += this.frameMs; }
      const hangMs = (!this.awaiting && this.voiceMs > 0 && this.voiceMs <= this.cfg.shortCmdMax)
        ? this.cfg.hangShort : this.cfg.hangFull;
      const capMs = this.capLen / this.rate * 1000;
      if (this.silenceMs >= hangMs || capMs >= this.cfg.maxMs) {
        this.capturing = false;
        if (this.voiceMs >= this.cfg.minVoice) {
          const out = this.cap.slice(0, this.capLen);              // one copy per utterance, transferred
          this.port.postMessage({ type: 'end', samples: out, hangMs: hangMs, capMs: Math.round(capMs) }, [out.buffer]);
        } else {
          this.port.postMessage({ type: 'drop', voiceMs: Math.round(this.voiceMs), capMs: Math.round(capMs) });
        }
        this.capLen = 0; this.voiceMs = 0; this.silenceMs = 0; this.ringPos = 0; this.ringFilled = 0;
      }
    }
  }
}
registerProcessor('vad-processor', VadProcessor);
