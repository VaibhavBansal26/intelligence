// Workaround wrapper for `npm run seed` in the Funnels repo, which currently crashes with
// "ReferenceError: Cannot access 'ListQuery' before initialization" (circular import entered via seed.ts).
// Importing the app module graph first (the order src/index.ts uses) avoids the cycle. Usage:
//   FUNNELS_SERVER_DIR=<repo>/server DATABASE_PATH=... npx --prefix <repo>/server tsx seed_funnels.mjs
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const dir = process.env.FUNNELS_SERVER_DIR;
if (!dir) throw new Error('FUNNELS_SERVER_DIR is required');
const imp = (p) => import(pathToFileURL(path.join(dir, 'src', p)).href);
await imp('app.ts');
const { loadConfig } = await imp('config.ts');
const { createDeps } = await imp('bootstrap.ts');
const { seedDemo } = await imp('seed.ts');
const config = loadConfig();
if (config.NODE_ENV === 'production') throw new Error('refusing to seed in production');
const deps = await createDeps(config);
const r = await seedDemo(deps);
console.log(JSON.stringify(r));
await deps.db.close();
