/**
 * Opens https://redbrick.coffee/, lets Stagehand find the shop page on its
 * own, then records every product and its price.
 *
 * Run:  OPENAI_API_KEY=sk-... pnpm scrape
 *
 * Results are printed as a table and saved to output/ as JSON and CSV.
 */
import { mkdir, writeFile } from "node:fs/promises";
import { localBrowser, Stagehand, type ModelName } from "@browserbasehq/stagehand";
import { z } from "zod/v4";

const START_URL = "https://redbrick.coffee/";
const MAX_NAVIGATION_STEPS = 4; // act() attempts allowed to reach the shop page
const MAX_PAGES = 10; // safety cap if the shop is paginated

// ---------------------------------------------------------------------------
// Model configuration. Stagehand never reads env vars itself, so we do it here.
// ---------------------------------------------------------------------------
const MODEL_NAME = (process.env.STAGEHAND_MODEL ?? "openai/gpt-5.6-sol") as ModelName;
const API_KEY_ENV: Record<string, string> = {
  openai: "OPENAI_API_KEY",
  anthropic: "ANTHROPIC_API_KEY",
  google: "GOOGLE_API_KEY",
  groq: "GROQ_API_KEY",
  cerebras: "CEREBRAS_API_KEY",
};

function modelApiKey(): string {
  const provider = MODEL_NAME.split("/")[0];
  const envVar = API_KEY_ENV[provider];
  const key = envVar ? process.env[envVar] : undefined;
  if (!key) {
    throw new Error(`Set ${envVar ?? "an API key env var"} for model "${MODEL_NAME}".`);
  }
  return key;
}

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------
const PageCheckSchema = z.object({
  isShopPage: z
    .boolean()
    .describe(
      "true only if this is the store's dedicated shop / catalogue page listing its products for sale with prices. " +
        "The homepage is NOT the shop page, even if it shows a few featured products.",
    ),
  reason: z.string().describe("One short sentence explaining the decision."),
});

const ProductListSchema = z.object({
  products: z.array(
    z.object({
      name: z.string().describe("Product name as shown on the product card"),
      price: z
        .string()
        .describe("Current selling price exactly as displayed, including any 'From' prefix, e.g. 'From $15.00' or '$47.00'"),
      originalPrice: z
        .string()
        .nullable()
        .describe("Crossed-out regular price ONLY if the product is visibly on sale; otherwise null. Ignore hidden $0.00 placeholders."),
      soldOut: z.boolean().describe("true if the card says sold out / unavailable"),
      url: z.string().nullable().describe("Link to the product's page, if present"),
    }),
  ),
  currency: z
    .string()
    .nullable()
    .describe("Currency code the prices are shown in (e.g. AUD, USD) if the page indicates it, otherwise null"),
  hasNextPage: z
    .boolean()
    .describe("true if there is a pagination 'Next' link or 'Load more' button that reveals more products"),
});

type Product = z.infer<typeof ProductListSchema>["products"][number];

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

/**
 * Let Stagehand discover the shop page by itself. Each loop iteration asks
 * act() to take one step towards it (e.g. open a collapsed menu, then click
 * the right link) and asks extract() whether we've arrived.
 */
async function discoverShopPage(stagehand: Stagehand, currentUrl: () => Promise<string>) {
  for (let step = 1; step <= MAX_NAVIGATION_STEPS; step++) {
    const { data: action } = await stagehand.act(
      "Take the next step to get to this store's shop page, where all of its products are listed for sale. " +
        "If the navigation links are hidden behind a menu button, open the menu first.",
    );
    console.log(`  step ${step}: ${action.actionDescription || action.message}`);

    const { data: check } = await stagehand.extract(
      "Decide whether the current page is the store's shop page.",
      PageCheckSchema,
    );
    console.log(`  -> ${check.isShopPage ? "shop page found" : "not there yet"}: ${check.reason}`);

    if (check.isShopPage) return currentUrl();
  }
  throw new Error(`Couldn't find the shop page within ${MAX_NAVIGATION_STEPS} steps.`);
}

/** Extract products from the current page, following pagination if present. */
async function extractAllProducts(stagehand: Stagehand) {
  const products = new Map<string, Product>();
  let currency: string | null = null;

  for (let pageNum = 1; pageNum <= MAX_PAGES; pageNum++) {
    const { data } = await stagehand.extract(
      "Extract every product listed on this shop page with its name, price, sale price, availability and link.",
      ProductListSchema,
    );
    currency ??= data.currency;

    const before = products.size;
    for (const p of data.products) products.set(p.url ?? p.name, p); // de-dupe across pages
    console.log(`  page ${pageNum}: ${data.products.length} products (${products.size - before} new)`);

    if (!data.hasNextPage || products.size === before) break;
    await stagehand.act("Go to the next page of products (click 'Next' or 'Load more').");
  }

  return { products: [...products.values()], currency };
}

/** Pull the numeric amount out of a display price like "From $15.00". */
function parseAmount(price: string | null): number | null {
  const match = price?.replace(/,/g, "").match(/\d+(\.\d+)?/);
  return match ? Number(match[0]) : null;
}

async function saveResults(shopUrl: string, currency: string | null, products: Product[]) {
  const scrapedAt = new Date().toISOString();
  const rows = products.map((p) => ({
    name: p.name,
    price: p.price,
    amount: parseAmount(p.price),
    priceFrom: /^\s*from\b/i.test(p.price), // "From $X" = several sizes/variants
    originalPrice: p.originalPrice,
    onSale: p.originalPrice !== null,
    soldOut: p.soldOut,
    url: p.url ? new URL(p.url, shopUrl).href : null,
  }));

  await mkdir("output", { recursive: true });
  const stamp = scrapedAt.replace(/[:.]/g, "-");
  const base = `output/redbrick-products-${stamp}`;

  await writeFile(`${base}.json`, JSON.stringify({ scrapedAt, shopUrl, currency, products: rows }, null, 2));

  const header = Object.keys(rows[0] ?? { name: "" });
  const csvCell = (v: unknown) => (v === null ? "" : `"${String(v).replace(/"/g, '""')}"`);
  const csv = [header.join(","), ...rows.map((r) => header.map((h) => csvCell(r[h as keyof typeof r])).join(","))];
  await writeFile(`${base}.csv`, csv.join("\n") + "\n");

  console.table(rows.map(({ name, price, originalPrice, soldOut }) => ({ name, price, originalPrice, soldOut })));
  console.log(`\nCurrency: ${currency ?? "not shown"}`);
  console.log(`Saved ${rows.length} products to ${base}.json and ${base}.csv`);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
  const browser = await localBrowser.launch({
    headless: process.env.HEADLESS === "true", // visible window by default, like the quickstart
  });
  try {
    const stagehand = await Stagehand.create({
      browser,
      model: { modelName: MODEL_NAME, apiKey: modelApiKey() },
    });
    try {
      const [page] = await browser.context.pages();

      console.log(`Opening ${START_URL}`);
      await page.goto(START_URL, { waitUntil: "domcontentloaded" });

      console.log("Looking for the shop page...");
      const shopUrl = await discoverShopPage(stagehand, () => page.url());
      console.log(`Shop page: ${shopUrl}\n`);

      console.log("Extracting products...");
      const { products, currency } = await extractAllProducts(stagehand);
      if (products.length === 0) throw new Error("No products found on the shop page.");

      await saveResults(shopUrl, currency, products);
    } finally {
      await stagehand.close();
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
