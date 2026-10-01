export const items = new Map();

export async function getItemAsync(key) {
  return items.has(key) ? items.get(key) : null;
}
export async function setItemAsync(key, value) {
  items.set(key, String(value));
}
export async function deleteItemAsync(key) {
  items.delete(key);
}
