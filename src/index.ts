import "dotenv/config";
import express from "express";
import { PrismaPg } from "@prisma/adapter-pg";
import { PrismaClient } from "../generated/prisma/client.js";
import { getCleaningProcess } from "./cleaning.js";

const app = express();
const port = process.env["PORT"] ?? 3000;

const adapter = new PrismaPg({ connectionString: process.env["DATABASE_URL"] });
const prisma = new PrismaClient({ adapter });

app.use(express.json());

app.get("/", (_req, res) => {
  res.json({ status: "ok" });
});

// Cleaning process(es) for a chemical by name or synonym.
//   e.g. GET /cleaning/acetic%20acid
app.get("/cleaning/:name", async (req, res) => {
  try {
    const results = await getCleaningProcess(prisma, req.params.name);
    if (results.length === 0) {
      res.status(404).json({ error: `No chemical found for "${req.params.name}"` });
      return;
    }
    // Only the source copies that actually carry cleaning steps are useful.
    const withCleaning = results.filter((c) => c.cleaningProcesses.length > 0);
    res.json({
      query: req.params.name,
      matched: results.map((c) => ({ id: c.id, canonical_name: c.canonical_name })),
      cleaning: withCleaning.map((c) => ({
        cargo_id: c.id,
        canonical_name: c.canonical_name,
        source: c.source?.name ?? null,
        processes: c.cleaningProcesses,
      })),
    });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Internal server error" });
  }
});

app.listen(port, () => {
  console.log(`Server listening on http://localhost:${port}`);
});
