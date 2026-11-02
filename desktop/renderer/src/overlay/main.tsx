import React from 'react';
import { createRoot } from 'react-dom/client';
import { Overlay } from './Overlay';
import './overlay.css';

const container = document.getElementById('overlay-root');
if (!container) throw new Error('Overlay root element missing');

createRoot(container).render(
  <React.StrictMode>
    <Overlay />
  </React.StrictMode>,
);
