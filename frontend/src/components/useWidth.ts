import { useEffect, useRef, useState } from 'react'

// 量測元素寬度，讓 SVG 依實際寬度繪製（文字不會被縮放變形）
export function useWidth<T extends HTMLElement>(initial = 600) {
  const ref = useRef<T | null>(null)
  const [width, setWidth] = useState(initial)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width
      if (w && Math.abs(w - width) > 0.5) setWidth(w)
    })
    ro.observe(el)
    setWidth(el.getBoundingClientRect().width || initial)
    return () => ro.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return [ref, width] as const
}
