type ScholarPublication = {
  scholarId?: string;
  title: string;
  citations: number;
};

type ScholarSnapshot = {
  updatedAt: string | null;
  publications: ScholarPublication[];
};

type CuratedPublication = {
  scholarId?: string;
  title: string;
  citations: number;
};

export function normalizeScholarTitle(title: string): string {
  return title.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
}

export function publicationCitations(
  publication: CuratedPublication,
  snapshot: ScholarSnapshot,
): number | null {
  if (!snapshot.updatedAt) return publication.citations;

  const idMatch = publication.scholarId
    ? snapshot.publications.find((item) => item.scholarId === publication.scholarId)
    : undefined;
  if (idMatch) return idMatch.citations;

  const title = normalizeScholarTitle(publication.title);
  const matches = snapshot.publications.filter((item) => normalizeScholarTitle(item.title) === title);
  // Do not present an old or ambiguous citation count as a successful match.
  return matches.length === 1 ? matches[0].citations : null;
}

export function formatScholarUpdatedAt(updatedAt: string | null): string | null {
  if (!updatedAt || !Number.isFinite(Date.parse(updatedAt))) return null;
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(updatedAt));
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((value) => value.type === type)?.value;
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}`;
}

export function isScholarStale(updatedAt: string | null, now: number): boolean {
  if (!updatedAt) return false;
  const lastSuccess = Date.parse(updatedAt);
  return Number.isFinite(lastSuccess) && now - lastSuccess > 7 * 24 * 60 * 60 * 1000;
}
