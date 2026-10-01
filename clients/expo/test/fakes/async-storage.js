export const items = new Map();

export default {
  async getItem(key) {
    return items.has(key) ? items.get(key) : null;
  },
  async setItem(key, value) {
    items.set(key, String(value));
  },
  async removeItem(key) {
    items.delete(key);
  },
};
