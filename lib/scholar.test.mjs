import assert from 'node:assert/strict';
import test from 'node:test';
import { formatScholarUpdatedAt, isScholarStale, normalizeScholarTitle, publicationCitations } from './scholar.ts';

const publication = { title: 'Physics-Informed Neural Networks: A Method', citations: 11 };
const snapshot = (publications, updatedAt = '2026-09-25T16:30:00Z') => ({ updatedAt, publications });

test('matches titles across punctuation, whitespace, case, and Unicode compatibility forms', () => {
  assert.equal(normalizeScholarTitle('ＰＩＮＮ: α–β'), normalizeScholarTitle('pinn α β'));
  assert.equal(publicationCitations(publication, snapshot([
    { title: 'PHYSICS INFORMED NEURAL NETWORKS — A METHOD', citations: 15 },
  ])), 15);
});

test('prefers Scholar IDs when a title has changed', () => {
  assert.equal(publicationCitations({ ...publication, scholarId: 'stable-id' }, snapshot([
    { scholarId: 'stable-id', title: 'Updated publication title', citations: 16 },
    { title: publication.title, citations: 3 },
  ])), 16);
});

test('does not disguise old or ambiguous citation counts as newly synchronized', () => {
  assert.equal(publicationCitations(publication, snapshot([])), null);
  assert.equal(publicationCitations(publication, snapshot([
    { title: publication.title, citations: 2 },
    { title: publication.title, citations: 3 },
  ])), null);
  assert.equal(publicationCitations(publication, snapshot([], null)), 11);
  assert.equal(publicationCitations(publication, snapshot([{ title: publication.title, citations: 0 }])), 0);
});

test('formats successful sync time in Asia/Shanghai across date boundaries', () => {
  assert.equal(formatScholarUpdatedAt('2026-09-25T16:30:00Z'), '2026-09-26 00:30');
  assert.equal(formatScholarUpdatedAt(null), null);
  assert.equal(formatScholarUpdatedAt('invalid'), null);
});

test('retained-data indicator starts only after seven days since a successful sync', () => {
  const timestamp = '2026-09-25T00:00:00Z';
  const sevenDaysLater = Date.parse(timestamp) + 7 * 24 * 60 * 60 * 1000;
  assert.equal(isScholarStale(timestamp, sevenDaysLater), false);
  assert.equal(isScholarStale(timestamp, sevenDaysLater + 1), true);
  assert.equal(isScholarStale(null, sevenDaysLater), false);
});
