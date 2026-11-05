"""Let Stagehand find the Redbrick Coffee shop page, then record its products and prices."""

import asyncio
import csv
import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from stagehand import Stagehand, local_browser

load_dotenv()


class PageCheck(BaseModel):
    is_shop_page: bool = Field(description="True only on the store's shop page listing its products for sale, not the homepage")
    reason: str


class Product(BaseModel):
    name: str
    price: str = Field(description="Price as displayed, e.g. 'From $15.00'")
    original_price: str | None = Field(description="Crossed-out price if visibly on sale, else null")
    sold_out: bool
    url: str | None


class Products(BaseModel):
    products: list[Product]


async def main() -> None:
    api_key = os.environ["GOOGLE_GENERATIVE_AI_API_KEY"]
    model = "google/" + os.getenv("GEMINI_MODEL", "gemini-3.8-flash").removeprefix("google/")
    browser = await local_browser.launch(headless=os.getenv("HEADLESS") == "true")
    try:
        stagehand = await Stagehand.create(browser=browser, model=model, model_api_key=api_key)
        try:
            page = (await browser.context.pages())[0]
            await page.goto("https://redbrick.coffee/")

            # Stagehand finds the shop page itself: take a step, then check whether we've arrived.
            for _ in range(4):
                step = await stagehand.act(
                    "Take the next step to get to this store's shop page, where all its products are listed "
                    "for sale. If the navigation links are hidden behind a menu button, open the menu first."
                )
                check = await stagehand.extract("Is the current page the store's shop page?", PageCheck)
                print(f"{step.data.action_description} -> {check.data.reason}")
                if check.data.is_shop_page:
                    break
            else:
                raise RuntimeError("Couldn't find the shop page")

            result = await stagehand.extract("Extract every product on this page with its price.", Products)
            products = result.data.products

            os.makedirs("output", exist_ok=True)
            with open("output/redbrick_products.csv", "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=Product.model_fields)
                writer.writeheader()
                writer.writerows(p.model_dump() for p in products)

            for p in products:
                print(f"{p.name}: {p.price}")
            print(f"\nSaved {len(products)} products from {await page.url()} to output/redbrick_products.csv")
        finally:
            await stagehand.close()
    finally:
        await browser.close()


asyncio.run(main())
