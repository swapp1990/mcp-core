// Packs the package, installs the tarball into fixture/ and bundles it with `expo export --platform ios`.
// `--node-modules <dir>` borrows an Expo 54 app's node_modules (read-only) instead of a full install.
import { execSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmdirSync, rmSync, symlinkSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const fixture = join(root, "fixture");
const modules = join(fixture, "node_modules");
const { version } = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const tarball = join(root, `swapp1990-auth-expo-${version}.tgz`);
const flag = process.argv.indexOf("--node-modules");
const borrowed = flag > 0 ? resolve(process.argv[flag + 1]) : null;

const sh = (command, cwd, env = {}) => execSync(command, { cwd, stdio: "inherit", env: { ...process.env, ...env } });

sh("npm pack", root);
rmSync(join(modules, "@swapp1990"), { recursive: true, force: true });

const links = [];
if (borrowed) {
  mkdirSync(modules, { recursive: true });
  for (const name of ["expo", "react", "react-native"]) {
    const link = join(modules, name);
    if (existsSync(link)) continue;
    symlinkSync(join(borrowed, name), link, "junction");
    links.push(link);
  }
  // Installed alone so npm never touches the borrowed tree; peers resolve from it through Metro.
  const staging = mkdtempSync(join(tmpdir(), "auth-expo-"));
  sh(`npm install --prefix "${staging}" --no-save --no-package-lock --legacy-peer-deps --no-audit --no-fund "${tarball}"`, root);
  cpSync(join(staging, "node_modules", "@swapp1990"), join(modules, "@swapp1990"), { recursive: true });
  rmSync(staging, { recursive: true, force: true });
} else {
  sh(`npm install --no-save --no-package-lock --no-audit --no-fund "${tarball}"`, fixture);
}

try {
  rmSync(join(fixture, "dist"), { recursive: true, force: true });
  sh("node node_modules/expo/bin/cli export --platform ios --output-dir dist", fixture, {
    CI: "1",
    EXPO_OFFLINE: "1",
    ...(borrowed ? { AUTH_EXPO_NODE_MODULES: borrowed } : {}),
  });
  console.log("check-metro: fixture bundled for iOS");
} finally {
  // Removes the link only (rmdir on a junction never touches its target); never rm -r through it.
  for (const link of links) (process.platform === "win32" ? rmdirSync : unlinkSync)(link);
}
