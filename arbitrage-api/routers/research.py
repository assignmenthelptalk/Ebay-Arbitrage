from fastapi import APIRouter
from pydantic import BaseModel

from services import margin_engine

router = APIRouter(prefix="/research", tags=["research"])


class MarginRequest(BaseModel):
    sale_price: float
    amazon_cost: float


@router.post("/margin", response_model=margin_engine.MarginCalcResult)
def calculate_margin(payload: MarginRequest) -> margin_engine.MarginCalcResult:
    return margin_engine.evaluate_margin(payload.sale_price, payload.amazon_cost)
