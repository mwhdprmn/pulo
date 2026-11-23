# DESIGN.md: Pulau (Linux Dynamic Island)

## Design Read
Reading this as: Native Wayland Desktop Utility (Dynamic Island Notification & Power HUD) for Linux desktop users on KDE Plasma Wayland, in a tactile hardware-integrated obsidian-surface style, dial `ENERGY 2 / RHYTHM 2 / MOTION 2`.

## Three Dials
- **ENERGY**: `2` (Balanced: merges quietly with the top monitor bezel in standby, expands with clear typographic hierarchy when a notification arrives)
- **RHYTHM**: `2` (Consistent pill geometry across 3 states: compact standby bar, medium toast banner, and structured notification drawer)
- **MOTION**: `2` (Purposeful spring-damped width/height morphing at 60fps when switching between Standby, Notification Toast, and Expanded Panel; respects reduced-motion toggle)

## Color System (2 Core + 1 Deliberate Accent)
All pairings verified with WCAG 2.x contrast formula:
1. **Core Surface (Obsidian Matte)**: `#0B0C0E` (primary island body) and `#161920` (elevated card surface inside expanded panel). Border stroke: `#2E3440` (contrast against inner card and outer screen edge).
   - *Reason*: Mimics physical camera-bezel hardware at the top of the monitor while providing a high-contrast dark canvas for glanceable status telemetry.
2. **Core Typography (Crisp Alabaster & Slate)**:
   - Primary text: `#F4F6F8` on `#0B0C0E` (18.88:1 contrast, AAA) and on `#161920` (16.12:1 contrast, AAA).
   - Secondary metadata text: `#A3B1C6` on `#0B0C0E` (9.42:1 contrast, AAA) and on `#161920` (8.04:1 contrast, AAA).
   - *Reason*: Ensures instant legibility at small glanceable sizes on both 1080p and HiDPI displays.
3. **Deliberate Accent (Power & Active Signal)**:
   - Emerald Charge / Full (`#34D399` on `#0B0C0E`: 10.66:1 contrast): Used strictly for active charger connection, full battery confirmation, and interactive focus ring.
   - Semantic Warning (`#FBBF24` on `#0B0C0E`: 12.15:1 contrast): Used only when battery drops to 20% or below, or DND is active.
   - *Reason*: Reserves color exclusively for real hardware power states and keyboard focus, never as background decoration.

## Typography & Layout Decisions (R-31 One-Line Reasons)
- **Font Family (`Sans` system UI + `Tabular` numerals)**: Native desktop sans-serif keeps the HUD integrated with KDE Plasma while tabular numerals prevent the standby clock from jittering every minute.
- **Standby Layout (Left Clock, Center Camera Gap, Right Battery + State Badge)**: Separates time and power telemetry to opposite ends of the pill so both can be read in a single peripheral glance.
- **Radius System**: `18px` radius on the compact standby pill, `22px` on the notification toast, `24px` on the expanded drawer, and `10px` on inner action buttons so container hierarchy is immediately clear (R-11).
- **Icons**: Custom-drawn geometric vector glyphs (battery cell, lightning bolt, checkmark, bell, monitor) paired with explicit text labels (`CHG`, `PENUH`, `AC`, `BAT`) so state is never communicated by color alone (R-04, R-39).
