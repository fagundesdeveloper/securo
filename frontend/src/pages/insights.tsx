import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Compass, Droplets, Target, CalendarCheck, RotateCcw } from 'lucide-react'

import { agents } from '@/lib/api'
import { ChatPanel } from '@/components/agents/chat-panel'
import { ActionPlans } from '@/components/insights/action-plans'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { ActionPlan } from '@/types'

/**
 * The analysis surface for the finance agent.
 *
 * Deliberately thin: an insight is a well-posed question put to the same
 * agent the chat uses, so the answer arrives through the existing streaming
 * pipeline and renders with the existing markdown, chart and proposal
 * components. Nothing here duplicates the chat — picking a card opens a
 * normal conversation that the user can keep arguing with.
 *
 * The prompts live in the frontend on purpose. They are editorial, not
 * logic: the agent's rules about which categories count and how to quote
 * numbers belong in its system prompt, where every channel gets them.
 */

interface Insight {
  key: string
  icon: React.ElementType
  prompt: string
}

const INSIGHTS: Insight[] = [
  {
    key: 'xray',
    icon: Compass,
    prompt: [
      'Faça um raio-X das minhas finanças dos últimos 3 meses fechados.',
      '',
      'Consulte as ferramentas para cada número. Quero, nesta ordem:',
      '1. Quanto entrou, quanto saiu e quanto sobrou por mês.',
      '2. Minha taxa de poupança: quanto por cento da renda sobrou.',
      '3. As 5 categorias que mais consomem, com o quanto cada uma',
      '   representa da minha renda.',
      '4. O que mudou de um mês para o outro que mereça atenção.',
      '5. As três coisas que, se eu mudasse, mexeriam mais no resultado —',
      '   em ordem de impacto em reais, não de facilidade.',
      '',
      'Se alguma conclusão depender de dado que está em "Não Reconhecido",',
      'diga isso em vez de arredondar por cima.',
    ].join('\n'),
  },
  {
    key: 'leaks',
    icon: Droplets,
    prompt: [
      'Onde meu dinheiro está vazando?',
      '',
      'Procure, com as ferramentas:',
      '- Cobranças recorrentes que eu talvez nem lembre que tenho:',
      '  assinatura, taxa, seguro, mensalidade. Para cada uma, diga quanto',
      '  custa por ano, não por mês.',
      '- Categorias onde eu gasto bem mais que a média dos meses anteriores.',
      '- Gastos pequenos e frequentes que somados viram um valor grande.',
      '- Juros, tarifa bancária e multa que eu paguei e poderia não ter pago.',
      '',
      'Ordene por quanto custa em 12 meses. Para cada item diga o que é',
      'fato (veio da ferramenta) e o que é suposição sua.',
    ].join('\n'),
  },
  {
    key: 'goals',
    icon: Target,
    prompt: [
      'Minhas metas cabem na minha renda?',
      '',
      'Liste minhas metas e, para cada uma: quanto falta, qual o prazo, e',
      'quanto eu precisaria guardar por mês para chegar lá. Compare com o',
      'que de fato sobrou por mês nos últimos 3 meses.',
      '',
      'Diga sem suavizar quais metas estão fora de alcance no ritmo atual,',
      'e quanto precisaria mudar — em reais por mês — para cada uma entrar',
      'no prazo. Se eu não tiver meta cadastrada, diga isso e sugira quais',
      'fariam sentido pelo meu padrão de gasto.',
    ].join('\n'),
  },
  {
    key: 'plan',
    icon: CalendarCheck,
    prompt: [
      'Monte um plano de ação para o mês que vem.',
      '',
      'Baseie-se no que eu de fato gastei nos últimos 3 meses, não em',
      'regra geral. Quero no máximo 5 ações, cada uma com:',
      '- o que fazer, em uma frase',
      '- quanto economiza ou rende por mês, em reais',
      '- como eu vou saber no fim do mês se cumpri',
      '',
      'Onde fizer sentido um teto de gasto por categoria, proponha o',
      'orçamento com a ferramenta de proposta, com o valor que você',
      'calculou — não um número redondo.',
    ].join('\n'),
  },
]

function reviewPrompt(plan: ActionPlan): string {
  return [
    `Avalie como foi o mês de ${plan.month.slice(0, 7)} contra o plano que eu`,
    'escrevi para ele. O plano foi:',
    '',
    plan.body,
    '',
    'Consulte as ferramentas e, para cada item do plano, diga: cumpri ou não,',
    'com o número que prova isso. Compare com os meses anteriores para dizer',
    'se melhorou ou piorou. No fim, uma nota de 0 a 10 para o mês e a única',
    'coisa que eu deveria levar para o plano seguinte.',
    '',
    'Não suavize o que não foi cumprido.',
  ].join('\n')
}

export default function InsightsPage() {
  const { t } = useTranslation()
  const [tab, setTab] = useState('analysis')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [initialPrompt, setInitialPrompt] = useState<string | null>(null)

  const { data: agent, isLoading, isError } = useQuery({
    queryKey: ['agents-default'],
    queryFn: () => agents.getDefault(),
    retry: false,
  })

  const start = (prompt: string) => {
    setConversationId(null)
    setInitialPrompt(prompt)
  }

  const reset = () => {
    setConversationId(null)
    setInitialPrompt(null)
  }

  // Asking for a verdict is the same act as asking for an insight: a fresh
  // conversation seeded with the question, so the answer lands in the chat
  // where it can be argued with before it is saved onto the plan.
  const requestReview = (plan: ActionPlan) => {
    start(reviewPrompt(plan))
    setTab('analysis')
  }

  if (isLoading) {
    return <div className="p-6 text-sm text-muted-foreground">{t('agents.loading')}</div>
  }

  // No agent at all (a fresh install, or every agent archived). The chat
  // cannot run without one, so point at where to make it instead of
  // rendering an input that would 404 on send.
  if (isError || !agent) {
    return (
      <div className="p-6">
        <PageHeader section={t('nav.groupAnalysis')} title={t('insights.title')} />
        <div className="rounded-xl border border-border bg-card p-6 text-sm text-muted-foreground">
          <p className="mb-3">{t('insights.noAgent')}</p>
          <Button asChild size="sm">
            <Link to="/agents">{t('insights.goToAgents')}</Link>
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 flex flex-col h-full min-h-0">
      <PageHeader
        section={t('nav.groupAnalysis')}
        title={t('insights.title')}
        action={
          initialPrompt && tab === 'analysis' ? (
            <Button variant="outline" size="sm" onClick={reset}>
              <RotateCcw size={14} className="mr-1.5" />
              {t('insights.newAnalysis')}
            </Button>
          ) : undefined
        }
      />

      <Tabs value={tab} onValueChange={setTab} className="flex-1 flex flex-col min-h-0">
        <TabsList className="mb-4 self-start">
          <TabsTrigger value="analysis">{t('insights.tabs.analysis')}</TabsTrigger>
          <TabsTrigger value="plans">{t('insights.tabs.plans')}</TabsTrigger>
        </TabsList>

        <TabsContent value="analysis" className="flex-1 flex flex-col min-h-0 mt-0">
          {!initialPrompt && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-6">
              {INSIGHTS.map(({ key, icon: Icon, prompt }) => (
                <button
                  key={key}
                  onClick={() => start(prompt)}
                  className="text-left rounded-xl border border-border bg-card p-4 hover:border-primary hover:bg-muted/40 transition-colors"
                >
                  <div className="flex items-center gap-2 mb-1.5">
                    <Icon size={16} className="text-primary" />
                    <p className="text-sm font-semibold text-foreground">
                      {t(`insights.cards.${key}.title`)}
                    </p>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {t(`insights.cards.${key}.description`)}
                  </p>
                </button>
              ))}
            </div>
          )}

          {/* min-h-0 so only the message list inside the panel scrolls. */}
          <div className="flex-1 min-h-0 rounded-xl border border-border bg-card overflow-hidden">
            <ChatPanel
              agent={agent}
              conversationId={conversationId}
              onConversationCreated={setConversationId}
              initialPrompt={initialPrompt}
              getPageContext={() => ({ route: '/insights', label: t('insights.title') })}
            />
          </div>
        </TabsContent>

        <TabsContent value="plans" className="flex-1 min-h-0 mt-0">
          <ActionPlans onRequestReview={requestReview} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
