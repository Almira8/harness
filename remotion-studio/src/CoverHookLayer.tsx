import React from 'react';

export type CoverHook = {text: string; startSec: number; endSec: number};

export function CoverHookLayer({text}: {text: string}) {
  return <div style={{
    position: 'absolute', top: '18%', left: '9%', right: '14%',
    display: 'flex', justifyContent: 'center', textAlign: 'center',
  }}>
    <div style={{
      maxWidth: '100%', padding: '0.3em 0.45em', borderRadius: '0.25em',
      background: '#101816ef', color: '#fff', fontSize: 'min(7vw, 5vh)',
      fontWeight: 800, lineHeight: 1.08, overflowWrap: 'anywhere',
      boxShadow: '0 6px 18px #0005', textShadow: '0 2px 4px #0005',
    }}>{text}</div>
  </div>;
}
