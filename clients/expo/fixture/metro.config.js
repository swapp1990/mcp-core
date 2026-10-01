const path = require("node:path");
const { getDefaultConfig } = require("expo/metro-config");

const config = getDefaultConfig(__dirname);
// Set by `check-metro --node-modules <dir>`: resolve everything but the package from an existing app.
const borrowed = process.env.AUTH_EXPO_NODE_MODULES;
if (borrowed) {
  config.resolver.nodeModulesPaths = [path.join(__dirname, "node_modules"), borrowed];
  config.watchFolders = [borrowed];
}
module.exports = config;
