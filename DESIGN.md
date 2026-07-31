# BioWorkflowManage Design System

## Direction

BioWorkflowManage is a light, desktop-first scientific engineering product. Its visual language is quiet and precise, with the density and familiar interaction vocabulary of a mature design tool. Decoration never competes with graph semantics, validation status, or the primary compile action.

## Theme

- Light theme only for the first product baseline.
- Pure white is the primary workspace surface.
- Secondary surfaces carry a barely perceptible teal tint.
- Depth is communicated through surface changes and hairline borders, not broad shadows.

## Color

All implementation colors use OKLCH.

| Token | Value | Role |
| --- | --- | --- |
| Background | `oklch(1 0 0)` | Canvas and top-level background |
| Surface | `oklch(0.975 0.006 165)` | Toolbars and secondary panels |
| Surface strong | `oklch(0.945 0.011 165)` | Selected and grouped regions |
| Ink | `oklch(0.22 0.025 165)` | Primary text |
| Muted | `oklch(0.48 0.018 165)` | Secondary text |
| Border | `oklch(0.90 0.012 165)` | Dividers and control outlines |
| Primary | `oklch(0.42 0.105 165)` | Primary action and current selection |
| Primary hover | `oklch(0.35 0.095 165)` | Hover and active primary states |
| Warning | `oklch(0.70 0.14 75)` | Warning state only |
| Error | `oklch(0.57 0.18 25)` | Error state only |

## Typography

- Interface: `system-ui`, `-apple-system`, `BlinkMacSystemFont`, `"Segoe UI"`, sans-serif.
- Technical identifiers: `"SFMono-Regular"`, `Consolas`, `"Liberation Mono"`, monospace.
- Fixed product UI scale: 0.75rem, 0.8125rem, 0.875rem, 1rem, 1.125rem.
- Use weight and spacing before adding larger type.

## Layout

- Desktop editor uses a four-part shell: header, library panel, workflow canvas, inspector.
- Spacing uses a 4px base: 4, 8, 12, 16, 24, 32.
- Panels meet edge-to-edge with hairline dividers. They are not floating cards.
- At narrower widths, secondary panels collapse before the canvas.
- Mobile is a readable overview, not a promise of full drag editing.

## Components

- Controls use 8px radii; product panels use 0-12px depending on containment.
- Primary buttons are filled deep teal with white text.
- Secondary buttons are white or surface-colored with a full hairline border.
- Focus rings are 2px teal with a 2px offset.
- Interactive transitions run for 150-200ms and communicate state only.

## Accessibility

- Target WCAG 2.2 AA.
- Do not encode diagnostics by color alone.
- Maintain visible focus and keyboard-reachable controls.
- Touch targets expand to at least 44px on coarse pointers.
- Respect `prefers-reduced-motion`.
