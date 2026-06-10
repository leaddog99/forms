"""One-off: generate a hero + per-step photos for the cookbook cook-view
prototype (forms/cookbook-cookview.html). Saves JPEGs under
GENERATED_DIR/cookbook/ so the app serves them at /generated/cookbook/<name>.jpg.
Throwaway helper — safe to delete."""
import io
import save_recipe_api as api
from image_gen_openai import _generate_image
from PIL import Image

OUT = api.GENERATED_DIR / "cookbook"
OUT.mkdir(parents=True, exist_ok=True)

STYLE = (" Warm editorial food photography, natural window light, rustic wooden "
         "surface, shallow depth of field, no text or logos.")

JOBS = [
    ("lasagna-hero",
     "A baked classic lasagna in a deep glass dish, golden bubbling browned "
     "cheese on top, one corner portion lifted out to show four layers of pasta, "
     "meat sauce and melted cheese, a sprig of basil and a linen napkin alongside."),
    ("lasagna-brown",
     "Ground beef and Italian sausage browning and breaking up in a large "
     "stainless steel skillet, a wooden spoon stirring, light steam rising, overhead."),
    ("lasagna-filling",
     "A glass mixing bowl of creamy ricotta cheese filling flecked with chopped "
     "parsley and grated parmesan, a spoon resting in it, on a marble counter."),
    ("lasagna-assemble",
     "Hands layering wide sheets of lasagna pasta over red meat sauce in a deep "
     "baking dish, mid-assembly with ricotta and mozzarella visible, overhead."),
    ("lasagna-ricotta",
     "A fine-mesh strainer lined with cheesecloth draining fresh ricotta cheese "
     "over a glass bowl, a small weight on top, on a marble counter."),
    ("lasagna-noodles",
     "Wide flat lasagna pasta sheets cooking in a large pot of salted boiling "
     "water, a few lifted with tongs, steam rising, overhead."),
    ("lasagna-bake",
     "A classic lasagna baking in the oven, golden and bubbling with browned "
     "cheese on top, in a deep glass baking dish."),
    ("lasagna-slice",
     "A single square slice of baked lasagna lifted out on a spatula, showing "
     "four neat layers of pasta, rich meat sauce and melted cheese, steam rising."),
]

for name, prompt in JOBS:
    out = OUT / f"{name}.jpg"
    if out.exists():
        print(f"[gen] {name} already exists — skipping", flush=True)
        continue
    print(f"[gen] {name} …", flush=True)
    raw = _generate_image(prompt + STYLE, orientation="landscape", quality="medium")
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.save(out, "JPEG", quality=85, optimize=True, progressive=True)
    print(f"[gen] saved /generated/cookbook/{name}.jpg ({img.width}x{img.height})", flush=True)

print("DONE", flush=True)
