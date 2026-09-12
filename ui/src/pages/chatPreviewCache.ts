/** Device-memory previews only. Every selection must still refresh from Core. */
export class ChatPreviewCache<T> {
  private readonly entries = new Map<string, T>();

  constructor(private readonly capacity = 10) {}

  get(id: string): T | undefined {
    const value = this.entries.get(id);
    if (value !== undefined) {
      this.entries.delete(id);
      this.entries.set(id, value);
    }
    return value;
  }

  set(id: string, value: T): void {
    this.entries.delete(id);
    this.entries.set(id, value);
    while (this.entries.size > this.capacity) {
      this.entries.delete(this.entries.keys().next().value!);
    }
  }

  delete(id: string): void { this.entries.delete(id); }
}
