# Phase 3: Frontend Implementation Plan (Final MVP)

## Goal Description
Create a modern, premium web interface for the AI Singer.
**Strategy**: Use **Monorepo** structure (`ui/` directory).
**Layout**: "Artifact-style" split-view layout. Chat on the left, **latest** Music Score on the right.

## Design Details

### 1. ChatWindow Design (Premium Dark)
- **Aesthetic**: Unified dark theme. Use deep grays (`#1a1a1a`), glowing accents, and glassmorphism for inputs. **Avoid parchment backgrounds.**
- **History**: Keep chat in local UI state only (no backend persistence). Refresh clears history.
- **Audio**: Inline audio player for the latest synthesis result.

### 2. History & State Management

| Type | Strategy |
| :--- | :--- |
| **Chat History** | **Local UI state only**. Not persisted; refresh clears it. |
| **Score View** | **Latest Only**. The preview panel always renders the most recent XML state. |
| **Audio Clipping** | **Latest Only**. UI reuses the most recent audio from the session. |

### 3. Split-View Interaction (Simplified)
- **No Sync**: Real-time note highlighting is skipped for MVP.
- **Static Preview**: The `ScorePreview` renders the latest MusicXML as a high-quality static sheet.
- **Modifications**: All chat instructions target the current (latest) score.

## Implementation Steps (Coding Agent)

1. **Initialize UI**: Create Vite project in `ui/` (React + TypeScript).
2. **Build API Client**: Connect to `POST /sessions`, `POST /sessions/{id}/upload`, `POST /sessions/{id}/chat`, `GET /sessions/{id}/audio`.
3. **Develop Split Layout**: Responsive flex/grid container.
4. **Score Viewer**: Integrate `OpenSheetMusicDisplay` for static rendering of the latest score.

## Verification Plan
1. **No Persistence**: Create session -> chat -> refresh -> history is cleared.
2. **Playback**: Synthesize once -> audio plays from the latest generation.
