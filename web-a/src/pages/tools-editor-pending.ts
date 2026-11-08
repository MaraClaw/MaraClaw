import { createContext, type Dispatch, type SetStateAction } from 'react'

export const ToolsEditorPending = createContext<Dispatch<SetStateAction<number>> | null>(null)
