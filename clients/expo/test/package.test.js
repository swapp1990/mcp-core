import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";

const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));

test("every export target exists and ships in the tarball", () => {
  const targets = Object.values(pkg.exports).flatMap((entry) => Object.values(entry));
  for (const target of [pkg.main, pkg.types, ...targets]) {
    assert.ok(existsSync(new URL(`../${target}`, import.meta.url)), `${target} is missing`);
    assert.ok(pkg.files.some((dir) => target.startsWith(`./${dir}/`)), `${target} is outside "files"`);
  }
});

test("the native SDK and its modules are peers, not bundled copies", () => {
  assert.equal(pkg.dependencies, undefined);
  for (const name of ["@logto/rn", "expo-secure-store", "expo-web-browser", "expo-crypto", "@react-native-async-storage/async-storage"]) {
    assert.ok(pkg.peerDependencies[name], `${name} must be a peer dependency`);
  }
  assert.equal(pkg.peerDependenciesMeta.react.optional, true);
});
