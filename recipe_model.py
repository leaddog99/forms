from pydantic import BaseModel, Field
from typing import List, Optional, Union
from datetime import datetime
import os

class AggregateRating(BaseModel):
    type: str = Field(default="AggregateRating", alias="@type")
    ratingValue: float
    reviewCount: int

class NutritionInfo(BaseModel):
    calories: Optional[str] = ""
    fatContent: Optional[str] = ""
    carbohydrateContent: Optional[str] = ""
    proteinContent: Optional[str] = ""

class HowToStep(BaseModel):
    type: str = Field(default="HowToStep", alias="@type")
    position: int
    name: Optional[str] = None
    text: str
    image: Optional[str] = None
    imageCredit: Optional[str] = None

class Tool(BaseModel):
    type: str = Field(default="HowToTool", alias="@type")
    name: str

class VideoObject(BaseModel):
    type: str = Field(default="VideoObject", alias="@type")
    name: str
    contentUrl: str
    thumbnailUrl: Optional[str] = ""
    uploadDate: Optional[str] = ""
    description: Optional[str] = ""

class Author(BaseModel):
    type: str = Field(default="Person", alias="@type")
    name: Optional[str] = ""
    image: Optional[str] = None

class Provenance(BaseModel):
    ethnicity: Optional[str] = ""
    originRegion: Optional[str] = ""
    firstDocumented: Optional[str] = None
    traditionalContext: Optional[str] = ""
    notableVariations: Optional[List[str]] = []
    relatedDishes: Optional[List[str]] = []
    sources: Optional[List[dict]] = []

class AccessControl(BaseModel):
    visibility: Optional[str] = ""
    sharedWith: Optional[List[str]] = []

class SourceInfo(BaseModel):
    type: Optional[str] = ""
    origin: Optional[str] = ""
    originalUrl: Optional[str] = ""

class RecipeModel(BaseModel):
    context: Optional[str] = Field(default="https://schema.org", alias="@context")
    type: Optional[str] = Field(default="Recipe", alias="@type")
    id: Optional[str] = ""
    name: Optional[str] = ""
    description: Optional[str] = ""
    image: Optional[List[str]] = []
    author: Optional[Author] = None
    datePublished: Optional[str] = ""
    dateModified: Optional[str] = ""
    recipeYield: Optional[str] = ""
    prepTime: Optional[str] = ""
    cookTime: Optional[str] = ""
    totalTime: Optional[str] = ""
    recipeCategory: Optional[str] = ""
    recipeCuisine: Optional[str] = ""
    keywords: Optional[List[str]] = []
    aggregateRating: Optional[AggregateRating] = None
    nutrition: Optional[NutritionInfo] = None
    recipeIngredient: Optional[List[str]] = []
    recipeInstructions: Optional[List[HowToStep]] = []
    notes: Optional[str] = ""
    tags: Optional[List[str]] = []
    video: Optional[Union[VideoObject, str]] = None
    servingSuggestions: Optional[str] = ""
    cookingMethod: Optional[str] = ""
    equipment: Optional[List[Tool]] = []
    suitableForDiet: Optional[List[str]] = []
    provenance: Optional[Provenance] = None
    imageSource: Optional[str] = ""
    inputImage: Optional[str] = None
    _imported_from: Optional[str] = ""
    _editor_version: Optional[str] = ""
    _access: Optional[AccessControl] = None
    _source: Optional[SourceInfo] = None

    def is_nullish(self, value):
        return value in [None, "", [], {}, "null", "None"] or (
            isinstance(value, list) and all(self.is_nullish(v) for v in value)
        )

    def file_exists(self, path):
        try:
            return os.path.exists(path) and os.path.isfile(path)
        except:
            return False

    def needs_image_generation(self) -> bool:
        has_valid_image_file = any(
            isinstance(img, str) and not self.is_nullish(img) and self.file_exists(img)
            for img in self.image or []
        )
        return (
            not has_valid_image_file and
            self.is_nullish(self.imageSource)
        )

    def prefers_dish_image(self) -> bool:
        return (
            not self.is_nullish(self.name) and
            not self.is_nullish(self.recipeIngredient) and
            any(
                step and hasattr(step, "text") and isinstance(step.text, str) and step.text.strip()
                for step in self.recipeInstructions or []
            )
        )
