from typing import List, Optional, Union
from pydantic import BaseModel, Field


class NutritionInformation(BaseModel):
    calories: Optional[str]
    fatContent: Optional[str]
    carbohydrateContent: Optional[str]
    proteinContent: Optional[str]


class HowToStep(BaseModel):
    type_: str = Field(default="HowToStep", alias="@type")
    position: Optional[int] = None
    name: Optional[str] = None
    text: str
    image: Optional[str] = None
    imageCredit: Optional[str] = None

class Author(BaseModel):
    type_: str = Field(default="Person", alias="@type")
    name: str
    image: Optional[str] = None


class AggregateRating(BaseModel):
    type_: str = Field(default="AggregateRating", alias="@type")
    ratingValue: float
    reviewCount: int


class EquipmentItem(BaseModel):
    type_: str = Field(default="HowToTool", alias="@type")
    name: str


class History(BaseModel):
    ethnicity: Optional[str]
    originRegion: Optional[str]
    firstDocumented: Optional[str]
    traditionalContext: Optional[str]
    notableVariations: Optional[List[str]]
    relatedDishes: Optional[List[str]]
    sources: Optional[List[str]]


class RecipeModel(BaseModel):
    context: Optional[str] = Field(alias="@context")
    type_: str = Field(default="Recipe", alias="@type")
    id: str
    name: str
    description: str
    image: List[str]
    author: Author
    datePublished: str
    dateModified: Optional[str]
    recipeYield: str
    prepTime: str
    cookTime: str
    totalTime: str
    recipeCategory: str
    recipeCuisine: str
    keywords: List[str]
    aggregateRating: Optional[AggregateRating]
    nutrition: NutritionInformation
    recipeIngredient: List[str]
    recipeInstructions: List[HowToStep]
    notes: Optional[str]
    tags: Optional[List[str]]
    video: Optional[str]
    servingSuggestions: Optional[str]
    cookingMethod: Optional[str]
    equipment: Optional[List[EquipmentItem]]
    suitableForDiet: Optional[List[str]]
    history: Optional[History]
    _imported_from: Optional[str]
    _editor_version: Optional[str]
    _access: Optional[dict]
    _source: Optional[dict]

    class Config:
        populate_by_name = True
        allow_population_by_field_name = True