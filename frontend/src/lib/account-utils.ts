export function getAccountName(account: { name: string; display_name?: string | null }): string {
  return account.display_name ?? account.name
}

/**
 * Return a presentation-only copy ordered by the name users see.
 * The input is never mutated, which keeps React Query's cached account list intact.
 */
export function sortAccountsByDisplayName<
  T extends { name: string; display_name?: string | null },
>(accounts: readonly T[]): T[] {
  return [...accounts].sort((left, right) =>
    getAccountName(left).localeCompare(getAccountName(right), undefined, {
      numeric: true,
      sensitivity: 'base',
    }),
  )
}

/**
 * The bank's identifier for an account, masked to its last 4 chars, e.g. "•••• 1234".
 *
 * Banks commonly report every account under the same label (often the holder's
 * name), so this is what tells two of them apart. Null when the provider gave us
 * no identifier, so callers render nothing rather than an empty mask. Locale-neutral
 * by construction: dots and the bank's own digits, nothing to translate.
 */
export function formatAccountMask(account: { masked_number?: string | null }): string | null {
  return account.masked_number ? `•••• ${account.masked_number}` : null
}

/**
 * Account name with its mask appended, e.g. "Checking •••• 1234", for compact
 * single-line surfaces such as the account <select> options, where there is no
 * room for a secondary line. When the account has an explicit display_name, the
 * mask is omitted since the user-chosen label already distinguishes it.
 */
export function getAccountLabel(account: {
  name: string
  display_name?: string | null
  masked_number?: string | null
}): string {
  const name = getAccountName(account)
  // When the account has an explicit display_name the user set, it already
  // distinguishes this account from others so the mask suffix is redundant.
  if (account.display_name) return name
  const mask = formatAccountMask(account)
  return mask ? `${name} ${mask}` : name
}

/**
 * Account types offered in the create/edit dialogs, and the single source for
 * turning a stored `type` into a translated label.
 */
export const ACCOUNT_TYPE_OPTIONS = [
  { value: 'checking', labelKey: 'accounts.typeChecking' },
  { value: 'savings', labelKey: 'accounts.typeSavings' },
  { value: 'credit_card', labelKey: 'accounts.typeCreditCard' },
  { value: 'investment', labelKey: 'accounts.typeInvestment' },
  { value: 'wallet', labelKey: 'accounts.typeWallet' },
] as const

/**
 * i18n key for an account type's label, or null for a type we don't have a
 * label for — callers render nothing rather than a raw key like
 * "accounts.typeFoo".
 */
export function getAccountTypeLabelKey(type: string | null | undefined): string | null {
  if (!type) return null
  return ACCOUNT_TYPE_OPTIONS.find((o) => o.value === type)?.labelKey ?? null
}
