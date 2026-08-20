import { describe, expect, it } from 'vitest'
import {
  ACCOUNT_TYPE_OPTIONS,
  getAccountLabel,
  getAccountTypeLabelKey,
  sortAccountsByDisplayName,
} from './account-utils'
import ptBR from '../locales/pt-BR.json'

describe('sortAccountsByDisplayName', () => {
  it('orders accounts by display name when present and falls back to name', () => {
    const accounts = [
      { id: '1', name: 'Alpha', display_name: 'Vacation' },
      { id: '2', name: 'Zulu', display_name: null },
      { id: '3', name: 'Checking', display_name: 'Bills' },
    ]

    expect(sortAccountsByDisplayName(accounts).map((account) => account.id)).toEqual([
      '3',
      '1',
      '2',
    ])
  })

  it('does not mutate the API account list', () => {
    const accounts = [{ name: 'Zulu' }, { name: 'Alpha' }]

    sortAccountsByDisplayName(accounts)

    expect(accounts.map((account) => account.name)).toEqual(['Zulu', 'Alpha'])
  })
})

describe('getAccountLabel', () => {
  it('appends the mask so same-named accounts can be told apart', () => {
    expect(getAccountLabel({ name: 'itau', masked_number: '2383' })).toBe('itau •••• 2383')
  })

  it('drops the mask once the user set their own name', () => {
    expect(
      getAccountLabel({ name: 'itau', display_name: 'Conta da Laís', masked_number: '2383' }),
    ).toBe('Conta da Laís')
  })
})

describe('getAccountTypeLabelKey', () => {
  it('resolves every type the edit dialog offers', () => {
    for (const option of ACCOUNT_TYPE_OPTIONS) {
      expect(getAccountTypeLabelKey(option.value)).toBe(option.labelKey)
    }
  })

  it('returns null instead of a raw key for unknown or missing types', () => {
    expect(getAccountTypeLabelKey('brokerage')).toBeNull()
    expect(getAccountTypeLabelKey(null)).toBeNull()
    expect(getAccountTypeLabelKey('')).toBeNull()
  })

  it('points at keys that actually exist in the catalogue', () => {
    // A missing key would surface as the literal "accounts.typeX" in the UI.
    const accounts = ptBR.accounts as unknown as Record<string, unknown>
    for (const option of ACCOUNT_TYPE_OPTIONS) {
      expect(typeof accounts[option.labelKey.replace('accounts.', '')]).toBe('string')
    }
  })
})
