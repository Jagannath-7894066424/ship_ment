import { spawn } from "node:child_process";
import path from "node:path";
import express from "express";
import { PrismaPg } from "@prisma/adapter-pg";
import { PrismaClient } from "../generated/prisma/client.js";
import { getCompleteProcedure, getProceduresForSource } from "./procedure.js";

/**
 * OPTIONAL HTTP surface over the procedure-template importer and query service.
 *
 * This project imports through the Python ETL scripts in etl/ — that is the real
 * import mechanism and this file does not replace it. It is a thin wrapper for
 * callers that need HTTP, and it deliberately shells out to the same importer
 * rather than reimplementing validation, transactions and idempotency in a
 * second place where the two could drift apart.
 *
 * Express is already a project dependency; nothing new is required. There is no
 * multipart handler here on purpose — accepting an upload needs a file-upload
 * middleware this project does not have, so the route takes a server-side path.
 * Add multer (or equivalent) if you want true browser uploads.
 *
 *   POST /procedure-templates/import   { "source_id": 24, "file_path": "..." }
 *   GET  /procedure-templates/:sourceId
 *   GET  /procedure-templates/:sourceId/:code
 *
 * Run: npx tsx src/procedure-import-api.ts
 */

const REPO_ROOT = path.resolve(__dirname, "..");
const IMPORTER = path.join(REPO_ROOT, "etl", "shell_procedure_templates.py");

export function createProcedureRouter(prisma: PrismaClient): express.Router {
  const router = express.Router();
  router.use(express.json());

  router.post("/procedure-templates/import", async (req, res) => {
    const { source_id: sourceId, file_path: filePath, dry_run: dryRun } = req.body ?? {};

    // source_id comes from the request, never from the spreadsheet: a procedure
    // code is meaningless without the document it was read from.
    if (!Number.isInteger(sourceId)) {
      return res.status(400).json({ error: "source_id is required and must be an integer" });
    }

    const args = [IMPORTER, "--source-id", String(sourceId)];
    if (typeof filePath === "string" && filePath) args.splice(1, 0, filePath);
    if (dryRun) args.push("--dry-run");

    const child = spawn("python3", args, { cwd: path.join(REPO_ROOT, "etl") });
    let out = "";
    child.stdout.on("data", (c) => (out += c));
    child.stderr.on("data", (c) => (out += c)); // the importer logs to stderr

    child.on("close", (code) => {
      // The importer runs in one transaction and rolls back on any error, so a
      // non-zero exit means nothing was written — report it as a failed import,
      // not a partial one.
      if (code === 0) return res.json({ imported: true, source_id: sourceId, log: out });
      res.status(422).json({ imported: false, source_id: sourceId, error: "import failed — no changes were written", log: out });
    });
  });

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
