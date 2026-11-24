import React from 'react';
import {AbsoluteFill, Easing, Img, Loop, OffthreadVideo, Sequence, interpolate,
  staticFile, useCurrentFrame, useVideoConfig} from 'remotion';

export type BackgroundCue = {
  startSec: number;
  endSec: number;
  kind: 'color' | 'image' | 'video';
  color?: string | null;
  src?: string | null;
  mediaDuration?: number | null;
};

function BackgroundPlate({cue, jobId}: {cue: BackgroundCue; jobId: string}) {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const progress = interpolate(frame, [0, Math.max(1, (cue.endSec - cue.startSec) * fps - 1)], [0, 1], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.ease),
  });
  const style = {width: '100%', height: '100%', objectFit: 'cover'} as const;
  const src = cue.src ? staticFile('assets/' + jobId + '/' + cue.src) : undefined;
  return <AbsoluteFill style={{backgroundColor: cue.color ?? undefined,
    scale: 1.04 + progress * 0.08, translate: (progress * 1.8 - 0.9) + '% 0'}}>
    {cue.kind === 'image' && src && <Img src={src} style={style}/>}
    {cue.kind === 'video' && src && cue.mediaDuration &&
      <Loop durationInFrames={Math.max(1, Math.floor(cue.mediaDuration * fps))}>
        <OffthreadVideo src={src} muted style={style}/>
      </Loop>}
  </AbsoluteFill>;
}

export function BackgroundLayer({cues, jobId}: {cues: BackgroundCue[]; jobId: string}) {
  const {fps, durationInFrames} = useVideoConfig();
  return <AbsoluteFill style={{overflow: 'hidden'}}>
    {cues.map((cue, index) => {
      const from = Math.round(cue.startSec * fps);
      const end = index === cues.length - 1 && Math.abs(cue.endSec * fps - durationInFrames) <= 1
        ? durationInFrames : Math.round(cue.endSec * fps);
      const duration = end - from;
      return duration > 0 && <Sequence key={cue.startSec} from={from} durationInFrames={duration} premountFor={fps}>
        <BackgroundPlate cue={cue} jobId={jobId}/>
      </Sequence>;
    })}
  </AbsoluteFill>;
}
