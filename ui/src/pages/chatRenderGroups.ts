export function groupByAssistantId<T extends { assistantId: string }>(items: T[]) {
  const grouped = new Map<string, T[]>();
  for (const item of items) {
    const current = grouped.get(item.assistantId);
    if (current) current.push(item);
    else grouped.set(item.assistantId, [item]);
  }
  return grouped;
}
