---
version: alpha
name: RuPochta Mail — Blueprint Routing
description: Calm editorial mail workspace built from ink, cobalt, paper and precise routing cues.
colors:
  primary: "#0B1D45"
  secondary: "#1F66E5"
  tertiary: "#F04453"
  neutral: "#FFFFFF"
  canvas: "#F4F8FF"
  blueprint: "#EAF1FF"
  border: "#CBDCF5"
  muted: "#5B6B86"
  cyan: "#35B8E8"
  success: "#137B59"
typography:
  display:
    fontFamily: Arial Narrow
    fontSize: 4rem
    fontWeight: 800
    lineHeight: 0.94
    letterSpacing: "-0.16rem"
  heading:
    fontFamily: Arial Narrow
    fontSize: 2rem
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "-0.06rem"
  body:
    fontFamily: Inter
    fontSize: 1rem
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "0rem"
  label:
    fontFamily: Inter
    fontSize: 0.75rem
    fontWeight: 750
    lineHeight: 1.35
    letterSpacing: "0rem"
  utility:
    fontFamily: ui-monospace
    fontSize: 0.625rem
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "0.05rem"
rounded:
  sm: 7px
  md: 10px
  lg: 14px
  xl: 28px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
components:
  button-primary:
    backgroundColor: "{colors.secondary}"
    textColor: "{colors.neutral}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: 12px
    height: 50px
  button-primary-hover:
    backgroundColor: "#1558CC"
    textColor: "{colors.neutral}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: 12px
    height: 50px
  input:
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.primary}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 12px
    height: 48px
  surface-auth:
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.primary}"
    rounded: "{rounded.xl}"
    padding: 40px
  surface-blueprint:
    backgroundColor: "{colors.blueprint}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
  surface-canvas:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
  metadata:
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.muted}"
    typography: "{typography.utility}"
    rounded: "{rounded.sm}"
    padding: 8px
  technical-highlight:
    backgroundColor: "{colors.cyan}"
    textColor: "{colors.primary}"
    typography: "{typography.utility}"
    rounded: "{rounded.sm}"
    padding: 8px
  border-plate:
    backgroundColor: "{colors.border}"
    textColor: "{colors.primary}"
    typography: "{typography.utility}"
    rounded: "{rounded.sm}"
    padding: 8px
  status-success:
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.success}"
    typography: "{typography.utility}"
    rounded: "{rounded.sm}"
    padding: 8px
---

## Overview

RuPochta Mail is a precise routing desk rather than another generic SaaS inbox. The
interface combines true white paper, blueprint-blue structure, deep ink text and one
signal-red routing accent. Generated illustrations show envelopes moving through a
real system; they explain the product without mascots, stock photography or borrowed
brand assets.

The visual tone is calm, direct and technical. Hierarchy comes from condensed display
type, generous negative space and physical tray/routing imagery—not glass panels,
decorative orbs or unsupported product claims.

## Colors

- **Ink (`#0B1D45`)** is the core text and dark-theme anchor.
- **Cobalt (`#1F66E5`)** is reserved for actions, focus, current location and routing.
- **Signal red (`#F04453`)** marks a route or exceptional state; it is never the sole
  carrier of meaning.
- **Canvas / blueprint** separate working layers without grey corporate flatness.
- **Cyan** is a small technical highlight, not a competing CTA color.

## Typography

Display headings use a condensed system face in uppercase to create an editorial,
operational tone. Body copy remains a native sans-serif stack for speed and Russian
readability. Monospace appears only in hostnames, counters and technical labels.

## Layout

Desktop authentication uses one wide shell with a blueprint narrative rail and a
single focused form. At tablet and mobile widths the narrative becomes a compact top
panel; credentials remain above the first long scroll. The signed-in client keeps its
three working layers—navigation, queue, reader—but aligns them to the same tokens.

Minimum target sizes are 44px. Desktop target viewport is 1536×1024; mobile target is
390×844, with explicit checks for horizontal overflow and off-viewport controls.

## Elevation & Depth

Use one broad blue-tinted shell shadow and small directional shadows on physical
routing plates. Nested cards do not each receive their own floating glass shadow.
Dark mode uses borders and tonal steps rather than glow.

## Shapes

Controls use 7–10px radii. Workspace cards use 14px. The public auth shell may use
28px as the sole large container. Pills are limited to status or compact metadata.

## Components

`button-primary` is the only high-emphasis action in the authentication form. Inputs
have persistent labels, visible focus rings and no icon-only placeholder semantics.
The code-native mail mark and generated routing/empty-state images are the approved
visual assets for this surface.

## Login reference update, 2026-08-03

The supplied illustrated Mail frames replace the earlier login artwork guidance for
the public authentication screen. Their Russian copy, RuPochta characters, pale blue
canvas and feature cards are the visual source of truth.

Desktop shows the illustrated rail beside the real authentication form. GSAP crossfades
the four supplied frames through opacity every three seconds. Reduced motion switches
frames without animation. At tablet and mobile widths the rail is
hidden so credentials remain immediately available.
This update does not change the signed-in mailbox workspace or its artwork rules.

## Do's and Don'ts

- Do show a real route, tray or delivery state with custom imagery.
- Do preserve native form semantics, focus order and API-bound element IDs.
- Do keep one dominant action and hide unavailable authentication methods.
- Do keep generated art decorative (`alt=""`) when adjacent copy conveys the meaning.
- Don't reuse the former raster RuPochta logo, cat assistant or upstream LM assets.
- Don't introduce purple gradients, neon, glassmorphism, mascots or remote stock art.
- Don't use motion to convey state without a static equivalent; honor reduced motion.
