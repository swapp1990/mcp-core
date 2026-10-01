const fakes = {
  "@logto/rn": "../fakes/logto-rn.js",
  "expo-secure-store": "../fakes/expo-secure-store.js",
  "@react-native-async-storage/async-storage": "../fakes/async-storage.js",
};

export async function resolve(specifier, context, next) {
  if (specifier in fakes) return { url: new URL(fakes[specifier], import.meta.url).href, shortCircuit: true };
  return next(specifier, context);
}
