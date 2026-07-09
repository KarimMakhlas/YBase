import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './styles.css'
// Loaded after styles.css so the design-system screen layout (ported from the
// YBase UI kit) wins any class collision with the legacy view rules.
import './ybase/app.css'
// Final product layer: shared polish and responsive composition across the
// marketing, auth, workspace, and data-dense surfaces.
import './ybase/premium.css'

createRoot(document.getElementById('root')).render(<App />)
