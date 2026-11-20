import { describe, expect, it } from 'vitest';
import { alertHeadline } from './state';

describe('alertHeadline', () => {
  it('names each alarm class', () => {
    expect(alertHeadline('weapon', 'confirmed', 'Hall')).toBe('Possible weapon in the Hall');
    expect(alertHeadline('fire', 'corroborated', 'Hall')).toBe('Fire confirmed in the Hall');
  });

  it('calls a robbery a theft (D26), never a generic threat', () => {
    expect(alertHeadline('robbery', 'confirmed', 'Hall')).toBe('Possible theft in the Hall');
    expect(alertHeadline('robbery', 'confirmed')).toBe('Possible theft');
  });

  it('falls back to a threat for an unknown type', () => {
    expect(alertHeadline('something', 'confirmed', 'Hall')).toBe('Possible threat in the Hall');
  });
});
