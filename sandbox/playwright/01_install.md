# Install (one-time)

Run from the project venv. Playwright is ~10MB; the bundled Chromium
binary it downloads is ~280MB on disk.

```powershell
# In the activated venv:
pip install playwright

# Download Chromium (skip firefox + webkit — we only need Chromium for
# now; can always grab the others later):
playwright install chromium
```

Verify:

```powershell
python -c "from playwright.sync_api import sync_playwright; print('ok')"
```

## Uninstall

```powershell
playwright uninstall          # removes browser binaries
pip uninstall playwright       # removes the Python package
```
