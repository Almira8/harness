import React from "react";
import { Video } from "@remotion/media";
import {
  AbsoluteFill,
  Composition,
  Sequence,
  interpolate,
  registerRoot,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

type Clip = {
  asset_id: string;
  start: number;
  source_start: number;
  duration: number;
  layout: "presenter" | "split" | "fullscreen";
  muted: boolean;
  zoom: number;
};

type Overlay = {
  start: number;
  end: number;
  title: string;
  body: string;
  animation: "rise" | "slide" | "pop" | "fade";
};

type Caption = { start: number; end: number; text: string };
type Props = {
  duration: number;
  clips: Clip[];
  overlays: Overlay[];
  captions: Caption[];
  assets: Record<string, string>;
  notes: string;
};

const Footage: React.FC<{ clip: Clip; src: string }> = ({ clip, src }) => {
  const { fps } = useVideoConfig();
  const frame = useCurrentFrame();
  const fullscreen = clip.layout === "fullscreen";
  return (
    <div
      style={{
        position: "absolute",
        overflow: "hidden",
        borderRadius: fullscreen ? 0 : 36,
        left: fullscreen ? 0 : 72,
        right: fullscreen ? 0 : 72,
        top: fullscreen ? 0 : clip.layout === "split" ? 820 : 720,
        bottom: fullscreen ? 0 : 350,
      }}
    >
      <Video
        src={src}
        trimBefore={Math.round(clip.source_start * fps)}
        premountFor={fps}
        muted={clip.muted}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          scale: interpolate(
            frame,
            [0, Math.max(1, clip.duration * fps - 1)],
            [1, clip.zoom],
            {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            },
          ),
        }}
      />
    </div>
  );
};

const Graphic: React.FC<{ overlay: Overlay }> = ({ overlay }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const entry = Math.min(
    0.4 * fps,
    Math.max(1, ((overlay.end - overlay.start) * fps) / 2),
  );
  return (
    <div
      style={{
        position: "absolute",
        top: 230,
        left: 100,
        right: 100,
        color: "#fff",
        textShadow: "0 2px 5px #000, 0 4px 12px #000b",
        opacity: interpolate(frame, [0, entry], [0, 1], {
          extrapolateRight: "clamp",
        }),
        translate:
          overlay.animation === "rise"
            ? interpolate(frame, [0, entry], ["0px 50px", "0px 0px"], {
                extrapolateRight: "clamp",
              })
            : overlay.animation === "slide"
              ? interpolate(frame, [0, entry], ["-80px 0px", "0px 0px"], {
                  extrapolateRight: "clamp",
                })
              : "0px 0px",
        scale:
          overlay.animation === "pop"
            ? interpolate(frame, [0, entry], [0.88, 1], {
                extrapolateRight: "clamp",
              })
            : 1,
      }}
    >
      {overlay.title && (
        <div
          style={{
            fontSize: 58,
            lineHeight: 1.12,
            fontWeight: 700,
            overflowWrap: "anywhere",
          }}
        >
          {overlay.title}
        </div>
      )}
      {overlay.body && (
        <div
          style={{
            fontSize: 38,
            lineHeight: 1.3,
            marginTop: 24,
            overflowWrap: "anywhere",
          }}
        >
          {overlay.body}
        </div>
      )}
    </div>
  );
};

const Subtitle: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  return (
    <div
      style={{
        position: "absolute",
        left: 90,
        right: 160,
        bottom: 240,
        textAlign: "center",
        fontSize: 52,
        fontWeight: 700,
        lineHeight: 1.2,
        color: "#132b23",
        background: "#fffef5ef",
        padding: "18px 24px",
        borderRadius: 22,
        opacity: interpolate(frame, [0, 3], [0.3, 1], {
          extrapolateRight: "clamp",
        }),
      }}
    >
      {text}
    </div>
  );
};

export const Reel: React.FC<Props> = ({
  clips,
  overlays,
  captions,
  assets,
}) => {
  const { fps } = useVideoConfig();
  return (
    <AbsoluteFill
      style={{ background: "#f6f5ef", fontFamily: "Arial, sans-serif" }}
    >
      {clips.map((clip, index) => (
        <Sequence
          key={index}
          from={Math.round(clip.start * fps)}
          durationInFrames={
            Math.round((clip.start + clip.duration) * fps) -
            Math.round(clip.start * fps)
          }
          premountFor={fps}
        >
          <Footage clip={clip} src={assets[clip.asset_id]} />
        </Sequence>
      ))}
      {overlays.map((overlay, index) => (
        <Sequence
          key={index}
          from={Math.round(overlay.start * fps)}
          durationInFrames={Math.max(
            1,
            Math.round((overlay.end - overlay.start) * fps),
          )}
          premountFor={fps}
        >
          <Graphic overlay={overlay} />
        </Sequence>
      ))}
      {captions.map((caption, index) => (
        <Sequence
          key={index}
          from={Math.round(caption.start * fps)}
          durationInFrames={Math.max(
            1,
            Math.round((caption.end - caption.start) * fps),
          )}
          premountFor={fps}
        >
          <Subtitle text={caption.text} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};

const Root = () => (
  <Composition
    id="Reel"
    component={Reel}
    width={1080}
    height={1920}
    fps={30}
    durationInFrames={600}
    defaultProps={{
      duration: 20,
      clips: [],
      overlays: [],
      captions: [],
      assets: {},
      notes: "",
    }}
    calculateMetadata={({ props }) => ({
      durationInFrames: Math.ceil(props.duration * 30),
    })}
  />
);

registerRoot(Root);
