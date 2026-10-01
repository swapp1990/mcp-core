import { register } from "node:module";

// Native modules can't load in Node; tests import src/ unchanged against these fakes.
register("./hooks.js", import.meta.url);
