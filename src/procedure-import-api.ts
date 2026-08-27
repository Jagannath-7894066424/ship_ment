import path from "node:path";
import express from "express";
import { PrismaPg } from "@prisma/adapter-pg";
import { PrismaClient } from "../generated/prisma/client.js";
import { getCompleteProcedure, getProceduresForSource } from "./procedure.js";

/**
 * OPTIONAL HTTP surface over the procedure-template query service.
 *
 * Express is already a project dependency; nothing new is required.
 *
 *   GET  /procedure-templates/:sourceId
 *   GET  /procedure-templates/:sourceId/:code
 *
 * There is no import route. It used to shell out to the Shell procedure
 * importer, which was removed permanently along with its data; the remaining
 * procedures (Dr Verwey, Drew Ameroid) are loaded by the Python ETL in etl/,
 * which is the only import mechanism this project has.
 *
 * Run: npx tsx src/procedure-import-api.ts
 */

export function createProcedureRouter(prisma: PrismaClient): express.Router {
  const router = express.Router();
  router.use(express.json());

  router.get("/procedure-templates/:sourceId", async (req, res) => {
    const sourceId = Number(req.params.sourceId);
    if (!Number.isInteger(sourceId)) return res.status(400).json({ error: "sourceId must be an integer" });
    res.json(await getProceduresForSource(prisma, sourceId));
  });

  router.get("/procedure-templates/:sourceId/:code", async (req, res) => {
    const sourceId = Number(req.params.sourceId);
    if (!Number.isInteger(sourceId)) return res.status(400).json({ error: "sourceId must be an integer" });

    const procedure = await getCompleteProcedure(prisma, sourceId, String(req.params.code));
    if (!procedure) {
      return res.status(404).json({ error: `source ${sourceId} defines no procedure ${req.params.code}` });
    }
    res.json(procedure);
  });

  return router;
}

// Standalone runner, so the file is usable as-is rather than only as an example.
if (process.argv[1] && __filename === path.resolve(process.argv[1])) {
  const prisma = new PrismaClient({
    adapter: new PrismaPg({ connectionString: process.env["DATABASE_URL"] }),
  });
  const app = express();
  app.use(createProcedureRouter(prisma));
  const port = Number(process.env["PORT"] ?? 3000);
  app.listen(port, () => console.log(`procedure API listening on :${port}`));
}
