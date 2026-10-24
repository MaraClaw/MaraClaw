import type { UseQueryResult } from '@tanstack/react-query'
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type RefObject } from 'react'

import { Textarea } from '@/components/ui/textarea'
import type { InstalledAgentSkill } from '@/lib/agent-skills-api'
import { filterSkillMentions, findSkillMention, insertSkillMention, resolveSkillMentionSegments } from '@/lib/chat/skill-mentions'
import { cn } from '@/lib/utils'

type SkillMentionInputProps = {
  readonly agentName: string
  readonly value: string
  readonly onValueChange: (value: string) => void
  readonly onSend: () => void
  readonly textareaRef: RefObject<HTMLTextAreaElement | null>
  readonly skillsQuery: UseQueryResult<readonly InstalledAgentSkill[]>
  readonly isCurrent: () => boolean
}

// Both layers must have identical glyph metrics, wrapping, padding and scrollbar space.
const composerTextClass = 'w-full whitespace-pre-wrap break-words rounded-xl border px-3.5 py-2.5 font-sans text-sm font-normal tracking-normal [scrollbar-gutter:stable]'

export function SkillMentionInput({ agentName, value, onValueChange, onSend, textareaRef, skillsQuery, isCurrent }: SkillMentionInputProps) {
  const id = useId()
  const [selection, setSelection] = useState({ start: value.length, end: value.length })
  const [focused, setFocused] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const [composing, setComposing] = useState(false)
  const composingRef = useRef(false)
  const [active, setActive] = useState(0)
  const wasOpenRef = useRef(false)
  const activeOptionRef = useRef<HTMLButtonElement | null>(null)
  const restoreRef = useRef<{ readonly text: string; readonly caret: number } | null>(null)
  const backdropRef = useRef<HTMLDivElement | null>(null)
  const mention = focused && !dismissed && !composing && selection.start === selection.end
    ? findSkillMention(value, selection.start) : null
  const open = mention !== null
  const options = mention ? filterSkillMentions(skillsQuery.data ?? [], mention.query) : []
  const choices = options
  const hasData = skillsQuery.data !== undefined
  const activeIndex = Math.min(active, Math.max(0, choices.length - 1))
  const current = choices[activeIndex]
  const { refetch, isStale } = skillsQuery

  function syncBackdropScroll() {
    const input = textareaRef.current
    const backdrop = backdropRef.current
    if (input && backdrop) {
      backdrop.scrollTop = input.scrollTop
      backdrop.scrollLeft = input.scrollLeft
    }
  }

  useLayoutEffect(syncBackdropScroll)

  useEffect(() => {
    const opening = open && !wasOpenRef.current
    wasOpenRef.current = open
    if (opening && isStale && isCurrent()) void refetch({ cancelRefetch: false })
  }, [open, isStale, isCurrent, refetch])

  useEffect(() => {
    const input = textareaRef.current
    if (!input) return
    // React's onSelect does not observe every native/programmatic caret change.
    const onSelectionChange = () => {
      if (isCurrent()) setSelection({ start: input.selectionStart, end: input.selectionEnd })
    }
    input.addEventListener('selectionchange', onSelectionChange)
    return () => input.removeEventListener('selectionchange', onSelectionChange)
  }, [isCurrent, textareaRef])

  useEffect(() => {
    if (current) activeOptionRef.current?.scrollIntoView({ block: 'nearest' })
  }, [current])

  useLayoutEffect(() => {
    const restore = restoreRef.current
    if (!restore || restore.text !== value || !isCurrent()) return
    restoreRef.current = null
    textareaRef.current?.focus()
    textareaRef.current?.setSelectionRange(restore.caret, restore.caret)
  }, [value, isCurrent, textareaRef])

  function pick(skill: InstalledAgentSkill) {
    if (!mention || composingRef.current || !isCurrent()) return
    const inserted = insertSkillMention(value, mention, skill)
    restoreRef.current = inserted
    setSelection({ start: inserted.caret, end: inserted.caret })
    setDismissed(true)
    onValueChange(inserted.text)
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (!isCurrent()) return
    if (composingRef.current || event.nativeEvent.isComposing || event.keyCode === 229) {
      if (event.key === 'Enter' || event.key === 'Tab') event.preventDefault()
      return
    }
    if (event.key === 'Escape' && open) {
      event.preventDefault()
      setDismissed(true)
      return
    }
    if (open && !event.shiftKey) {
      if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && choices.length) {
        event.preventDefault()
        setActive((activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length)
        return
      }
      if ((event.key === 'Enter' || event.key === 'Tab') && current) {
        event.preventDefault()
        pick(current)
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      onSend()
    }
  }

  return <div className="relative min-w-0">
    {open ? <section id={`${id}-skills`} aria-label="Installed skills" aria-busy={!hasData && skillsQuery.isFetching}
      className="absolute bottom-full left-0 z-20 mb-2 w-full max-w-xl overflow-hidden rounded-xl border border-border bg-card text-card-foreground shadow-elevated">
      <p className="px-3 py-2 text-sm font-medium">Installed skills</p>
      {hasData && skillsQuery.isError ? <p role="status" className="px-3 pb-2 text-sm text-muted-foreground">Could not refresh installed skills. Showing cached skills.</p> : null}
      {!hasData && skillsQuery.isFetching ? <p role="status" className="px-3 pb-3 text-sm text-muted-foreground">Loading installed skills…</p>
        : !hasData && skillsQuery.isError ? <p role="alert" className="px-3 pb-3 text-sm text-destructive">Could not load installed skills. Keep typing, or reopen @ to try again.</p>
          : choices.length ? <div className="max-h-[min(16rem,35dvh)] overflow-y-auto overscroll-contain p-1">
            {choices.map((skill, index) => <button key={skill.path} ref={index === activeIndex ? activeOptionRef : undefined}
              type="button" aria-current={index === activeIndex ? 'true' : undefined}
              className={cn('flex min-h-11 w-full flex-col gap-0.5 rounded-lg px-3 py-2 text-left text-sm hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
                index === activeIndex && 'bg-accent text-accent-foreground outline-2 -outline-offset-2 outline-ring')}
              onPointerDown={(event) => event.preventDefault()} onClick={() => pick(skill)} onFocus={() => setActive(index)}>
              <span className="break-all font-medium">@{skill.name}</span>
              <span className="break-all text-xs text-muted-foreground">{skill.path}</span>
            </button>)}
          </div> : <p role="status" className="px-3 pb-3 text-sm text-muted-foreground">
            {skillsQuery.data?.length ? 'No matching installed skills.' : 'No skills installed for this agent.'}
          </p>}
    </section> : null}
    <div className="relative">
      <div ref={backdropRef} aria-hidden="true" data-slot="skill-mention-backdrop"
        className={cn(composerTextClass, 'pointer-events-none absolute inset-0 overflow-x-hidden overflow-y-auto border-transparent text-transparent')}>
        {resolveSkillMentionSegments(value, skillsQuery.data ?? []).map((segment) => segment.skill
          ? <span key={segment.start} data-slot="skill-mention-chip" className="box-decoration-clone rounded-md bg-accent">{segment.text}</span>
          : segment.text)}{'\n'}
      </div>
      <Textarea ref={textareaRef} aria-label={`Message ${agentName}`} value={value} placeholder={`Message ${agentName}…`}
      className={cn(composerTextClass, 'relative max-h-32 overflow-y-auto')} onScroll={syncBackdropScroll}
      aria-controls={open ? `${id}-skills` : undefined} aria-describedby={`${id}-hint`} onKeyDown={onKeyDown}
      onChange={(event) => {
        if (!isCurrent()) return
        setSelection({ start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd })
        setDismissed(false)
        setActive(0)
        onValueChange(event.currentTarget.value)
      }}
      onSelect={(event) => setSelection({ start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd })}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      onCompositionStart={() => { composingRef.current = true; setComposing(true) }}
      onCompositionEnd={() => { composingRef.current = false; setComposing(false) }} />
    </div>
    <p id={`${id}-hint`} className="mt-1.5 text-xs text-muted-foreground">Type @ to use an installed skill.{open ? ' ↑↓ to choose · Enter or Tab to insert · Esc to close' : ''}</p>
    <span role="status" className="sr-only">{open && current ? `${choices.length} installed skills. @${current.name}, ${activeIndex + 1} of ${choices.length}.` : ''}</span>
  </div>
}
