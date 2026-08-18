import React, { useState } from 'react'
import { S } from './styleStr'

// Reproduces the DC template's `style-hover` behaviour: merge a hover style
// object on mouse-over. `style`/`hoverStyle` accept inline-CSS strings.
export function Hover({ tag: Tag = 'button', style, hoverStyle, children, ...rest }) {
  const [hovered, setHovered] = useState(false)
  return (
    <Tag
      {...rest}
      style={{ ...S(style), ...(hovered ? S(hoverStyle) : null) }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {children}
    </Tag>
  )
}
