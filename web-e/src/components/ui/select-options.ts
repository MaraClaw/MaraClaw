import * as React from 'react'

type SelectOption = {
  readonly key: string
  readonly index: number
  readonly label: string
  readonly disabled: boolean
  readonly group: string | null
  readonly groupLabel: string | null
}

type SelectSnapshot = {
  readonly label: string
  readonly selected: string
  readonly disabled: boolean
  readonly options: readonly SelectOption[]
}

export function useSelectOptions(selectRef: React.RefObject<HTMLSelectElement | null>) {
  const [snapshot, setSnapshot] = React.useState<SelectSnapshot>({ label: '', selected: '', disabled: false, options: [] })
  const sync = React.useCallback(() => {
    const select = selectRef.current
    if (!select) return
    const options = Array.from(select.options, (option, index) => {
      const group = option.parentElement instanceof HTMLOptGroupElement ? option.parentElement : null
      return {
        key: `option-${index}`,
        index,
        label: option.label,
        disabled: option.disabled || !!group?.disabled,
        group: group ? `group-${Array.from(select.children).indexOf(group)}` : null,
        groupLabel: group?.label ?? null,
      }
    })
    const label = Array.from(select.labels, (item) => item.textContent?.trim()).filter(Boolean).join(' ')
    const next = { label, selected: options[select.selectedIndex]?.key ?? '', disabled: select.matches(':disabled'), options }
    setSnapshot((previous) => JSON.stringify(previous) === JSON.stringify(next) ? previous : next)
  }, [selectRef])

  // RHF writes .value through its native ref during commits and reset().
  React.useLayoutEffect(sync)
  React.useEffect(() => {
    const select = selectRef.current
    if (!select) return
    const observer = new MutationObserver(sync)
    observer.observe(select, { subtree: true, childList: true, characterData: true, attributes: true })
    const reset = () => queueMicrotask(sync)
    const form = select.form
    form?.addEventListener('reset', reset)
    return () => {
      observer.disconnect()
      form?.removeEventListener('reset', reset)
    }
  }, [selectRef, sync])

  return { ...snapshot, sync }
}

export function SelectNativeTrigger({ onKeyDown, onClick, onBlur, onPointerDown, ...props }: React.ComponentProps<'select'>) {
  const typed = React.useRef(false)
  return React.createElement('select', {
    ...props,
    onKeyDown: (event) => {
      if (event.key === 'Enter' && typed.current) event.preventDefault()
      typed.current = event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey
      onKeyDown?.(event)
      // Run after Radix's composed handler, before the browser's native popup.
      if (!event.ctrlKey && !event.metaKey && !event.altKey
        && (event.key.length === 1 || ['Enter', 'ArrowUp', 'ArrowDown'].includes(event.key))) event.preventDefault()
    },
    onBlur: (event) => { typed.current = false; onBlur?.(event) },
    onPointerDown: (event) => { typed.current = false; onPointerDown?.(event) },
    onClick: (event) => { onClick?.(event); event.preventDefault() },
  })
}
