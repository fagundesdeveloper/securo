import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { CalendarCheck, Gavel, Plus, Trash2, X } from 'lucide-react'

import { actionPlans } from '@/lib/api'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { Markdown } from '@/components/agents/markdown'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import type { ActionPlan } from '@/types'

/**
 * The month's intentions, and the verdict on how it went.
 *
 * The verdict is written by the app, not by the agent: every agent write in
 * this codebase goes through a `propose_*` tool the user approves, and an
 * agent filling in its own report card unprompted would be the one exception.
 * So "ask for a review" hands the question to the chat, and saving the answer
 * stays a deliberate act.
 */

const TEXTAREA =
  'w-full rounded-lg border border-border bg-card px-3 py-2 text-sm text-foreground ' +
  'placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary resize-y'

/** First day of next month, in the `YYYY-MM-DD` the API expects. */
function nextMonth(): string {
  const now = new Date()
  const next = new Date(now.getFullYear(), now.getMonth() + 1, 1)
  return `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, '0')}-01`
}

interface Props {
  /** Hands the review question to the analysis chat. */
  onRequestReview: (plan: ActionPlan) => void
}

export function ActionPlans({ onRequestReview }: Props) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const qc = useQueryClient()
  const [composing, setComposing] = useState(false)
  const [month, setMonth] = useState(nextMonth())
  const [body, setBody] = useState('')
  // Which plan's verdict box is open, and what is in it.
  const [verdictFor, setVerdictFor] = useState<string | null>(null)
  const [verdict, setVerdict] = useState('')

  const { data: plans = [], isLoading } = useQuery({
    queryKey: ['action-plans'],
    queryFn: () => actionPlans.list(),
  })

  const refresh = () => qc.invalidateQueries({ queryKey: ['action-plans'] })

  const create = useMutation({
    mutationFn: () => actionPlans.create({ month, body }),
    onSuccess: () => {
      setComposing(false)
      setBody('')
      setMonth(nextMonth())
      refresh()
      toast.success(t('insights.plans.saved'))
    },
    onError: (error: unknown) => {
      const status = (error as { response?: { status?: number } })?.response?.status
      // The server refuses a second plan for a month it already has one for.
      toast.error(status === 409 ? t('insights.plans.duplicate') : t('insights.plans.saveError'))
    },
  })

  const saveVerdict = useMutation({
    mutationFn: ({ id, review }: { id: string; review: string }) =>
      actionPlans.update(id, { review }),
    onSuccess: () => {
      setVerdictFor(null)
      setVerdict('')
      refresh()
      toast.success(t('insights.plans.reviewSaved'))
    },
    onError: () => toast.error(t('insights.plans.saveError')),
  })

  const remove = useMutation({
    mutationFn: (id: string) => actionPlans.delete(id),
    onSuccess: () => {
      refresh()
      toast.success(t('insights.plans.deleted'))
    },
    onError: () => toast.error(t('insights.plans.saveError')),
  })

  const monthLabel = (iso: string) =>
    // Parsed as parts, not `new Date(iso)`: that reads the date as UTC and
    // shows the month before in any negative-offset timezone, which is every
    // Brazilian one.
    new Date(Number(iso.slice(0, 4)), Number(iso.slice(5, 7)) - 1, 1).toLocaleDateString(locale, {
      month: 'long',
      year: 'numeric',
    })

  if (isLoading) {
    return <div className="p-4 text-sm text-muted-foreground">{t('agents.loading')}</div>
  }

  return (
    <div className="space-y-4 overflow-y-auto">
      {!composing ? (
        <Button size="sm" onClick={() => setComposing(true)}>
          <Plus size={14} className="mr-1.5" />
          {t('insights.plans.new')}
        </Button>
      ) : (
        <div className="rounded-xl border border-border bg-card p-4 space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold text-foreground">{t('insights.plans.new')}</p>
            <button
              onClick={() => setComposing(false)}
              className="text-muted-foreground hover:text-foreground"
              aria-label={t('common.cancel')}
            >
              <X size={14} />
            </button>
          </div>
          <div>
            <Label className="text-xs text-muted-foreground mb-1 block">
              {t('insights.plans.month')}
            </Label>
            <input
              type="month"
              value={month.slice(0, 7)}
              onChange={(e) => setMonth(e.target.value ? `${e.target.value}-01` : nextMonth())}
              className="rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>
          <div>
            <Label className="text-xs text-muted-foreground mb-1 block">
              {t('insights.plans.body')}
            </Label>
            <textarea
              rows={6}
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder={t('insights.plans.bodyPlaceholder')}
              className={TEXTAREA}
            />
          </div>
          <Button
            size="sm"
            onClick={() => create.mutate()}
            disabled={!body.trim() || create.isPending}
          >
            {t('insights.plans.save')}
          </Button>
        </div>
      )}

      {plans.length === 0 && !composing && (
        <div className="rounded-xl border border-dashed border-border p-6 text-center">
          <CalendarCheck size={20} className="mx-auto mb-2 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">{t('insights.plans.empty')}</p>
        </div>
      )}

      {plans.map((plan) => (
        <div key={plan.id} className="rounded-xl border border-border bg-card p-4">
          <div className="flex items-start justify-between gap-3 mb-2">
            <div>
              <p className="text-sm font-semibold text-foreground capitalize">
                {monthLabel(plan.month)}
              </p>
              <p className="text-xs text-muted-foreground">
                {plan.review ? t('insights.plans.reviewed') : t('insights.plans.open')}
              </p>
            </div>
            <button
              onClick={() => remove.mutate(plan.id)}
              className="text-muted-foreground hover:text-rose-500 transition-colors"
              aria-label={t('common.delete')}
            >
              <Trash2 size={14} />
            </button>
          </div>

          <p className="text-sm text-foreground whitespace-pre-wrap">{plan.body}</p>

          {plan.review ? (
            <div className="mt-3 pt-3 border-t border-border">
              <p className="text-xs font-medium text-muted-foreground mb-1.5">
                {t('insights.plans.verdict')}
              </p>
              <Markdown>{plan.review}</Markdown>
              <button
                onClick={() => saveVerdict.mutate({ id: plan.id, review: '' })}
                className="mt-2 text-xs text-muted-foreground hover:text-foreground"
              >
                {t('insights.plans.clearReview')}
              </button>
            </div>
          ) : verdictFor === plan.id ? (
            <div className="mt-3 pt-3 border-t border-border space-y-2">
              <textarea
                rows={5}
                value={verdict}
                onChange={(e) => setVerdict(e.target.value)}
                placeholder={t('insights.plans.verdictPlaceholder')}
                className={TEXTAREA}
              />
              <div className="flex gap-2">
                <Button
                  size="sm"
                  onClick={() => saveVerdict.mutate({ id: plan.id, review: verdict })}
                  disabled={!verdict.trim() || saveVerdict.isPending}
                >
                  {t('insights.plans.saveReview')}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setVerdictFor(null)}>
                  {t('common.cancel')}
                </Button>
              </div>
            </div>
          ) : (
            <div className="mt-3 flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => onRequestReview(plan)}>
                <Gavel size={14} className="mr-1.5" />
                {t('insights.plans.askReview')}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setVerdictFor(plan.id)
                  setVerdict('')
                }}
              >
                {t('insights.plans.writeReview')}
              </Button>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
