export function normalizeText(text: string): string {
  if (!text) return '';
  return text
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .trim();
}

export function parseSearchQuery(query: string): { exactPhrases: string[]; terms: string[] } {
  if (!query) return { exactPhrases: [], terms: [] };

  const exactPhrases: string[] = [];
  const regex = /"([^"]*)"/g;
  let match;

  while ((match = regex.exec(query)) !== null) {
    if (match[1].trim()) {
      exactPhrases.push(normalizeText(match[1]));
    }
  }

  const remaining = query.replace(/"[^"]*"/g, '');
  const terms = remaining
    .split(/\s+/)
    .map(normalizeText)
    .filter((t) => t.length > 1);

  return { exactPhrases, terms };
}
