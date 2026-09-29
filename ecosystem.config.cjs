const path = require("path");

const root = __dirname;

// Use `python -m uvicorn` (not `.venv/bin/uvicorn`) so relocated venvs still
// work when console-script shebangs point at an old absolute path.
function uvicornApp({ name, dir, module, port }) {
  const venvPython = path.join(root, dir, ".venv", "bin", "python");
  return {
    name,
    cwd: path.join(root, dir),
    script: venvPython,
    args: `-m uvicorn ${module} --host 0.0.0.0 --port ${port}`,
    interpreter: "none",
    instances: 1,
    exec_mode: "fork",
    autorestart: true,
    max_restarts: 10,
    min_uptime: "10s",
    env: {
      PYTHONUNBUFFERED: "1",
    },
  };
}

module.exports = {
  apps: [
    uvicornApp({
      name: "bluweb",
      dir: "Bluweb",
      module: "bluweb_app.main:app",
      port: 8001,
    }),
    uvicornApp({
      name: "osint",
      dir: "OSINT",
      module: "osint_app.main:app",
      port: 8002,
    }),
    uvicornApp({
      name: "reddit",
      dir: "reddit_server",
      module: "reddit_app.main:app",
      port: 8003,
    }),
    uvicornApp({
      name: "telegram",
      dir: "telegram_poc",
      module: "telegram_app.main:app",
      port: 8004,
    }),
    // Postgres-only OSINT worker. SQLite mode already runs workers inside
    // the osint API process — do not start this unless OSINT_DATABASE_URL
    // is postgresql+psycopg://...
    //   pm2 start ecosystem.config.cjs --only osint-worker
    // {
    //   name: "osint-worker",
    //   cwd: path.join(root, "OSINT"),
    //   script: path.join(root, "OSINT", ".venv", "bin", "python"),
    //   args: "-m osint_app.worker_main",
    //   interpreter: "none",
    //   instances: 1,
    //   exec_mode: "fork",
    //   autorestart: true,
    //   max_restarts: 10,
    //   min_uptime: "10s",
    //   env: { PYTHONUNBUFFERED: "1" },
    // },
  ],
};
