import React from 'react';
import {BackgroundLayer, type BackgroundCue} from './BackgroundLayer';
import {getVideoMetadata} from '@remotion/media-utils';
import {AbsoluteFill, Audio, Composition, Easing, Img, OffthreadVideo, Sequence,
  interpolate, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';

type Span = {startSec: number; endSec: number};
type Zoom = Span & {fromScale: number; toScale: number};
type Insert = Span & {src: string; animation: 'fade' | 'slide_up'};
type Callout = Span & {title: string; subtitle: string | null};
type Word = Span & {word: string};
type Props = {
  backgrounds?: BackgroundCue[]; transparentAvatar?: boolean;
  jobId: string; avatar: string; zoom_events: Zoom[]; b_roll: Insert[];
  callouts: Callout[]; words: Word[]; music: string | null; volume: number;
  accent: string; subtitle_style: 'highlight' | 'minimal';
};
const media = (job: string, src: string) => staticFile('assets/' + job + '/' + src);
const cover: React.CSSProperties = {width:'100%',height:'100%',objectFit:'cover'};
const clamp = {extrapolateLeft:'clamp',extrapolateRight:'clamp'} as const;

export function BaseAvatarLayer(p: Props) {
  const t = useCurrentFrame() / useVideoConfig().fps;
  let scale = 1;
  for (const z of p.zoom_events) {
    if (t < z.startSec) break;
    scale = interpolate(t,[z.startSec,z.endSec],[z.fromScale,z.toScale],
      {...clamp,easing:Easing.out(Easing.ease)});
  }
  return <AbsoluteFill style={{overflow:'hidden'}}><OffthreadVideo src={media(p.jobId,p.avatar)} transparent={p.transparentAvatar}
    style={{...cover,transform:'scale('+scale+')'}}/></AbsoluteFill>;
}

function Timed({span,children}: {span:Span;children:React.ReactNode}) {
  const {fps} = useVideoConfig();
  const start = Math.round(span.startSec*fps);
  return <Sequence from={start} durationInFrames={Math.max(1,Math.round(span.endSec*fps)-start)}>
    {children}</Sequence>;
}

function InsertClip({item,job}: {item:Insert;job:string}) {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const length = Math.max(1, Math.round(item.endSec*fps)-Math.round(item.startSec*fps));
  const opacity = Math.min(1,(frame+1)/5,(length-frame)/5);
  const y = item.animation === 'slide_up' ? (1-opacity)*70 : 0;
  const src = media(job,item.src);
  return <AbsoluteFill style={{opacity,transform:'translateY('+y+'px)',background:'#121816'}}>
    {/\.(png|jpe?g|webp)$/i.test(item.src) ? <Img src={src} style={cover}/> :
      <OffthreadVideo src={src} muted style={cover}/>}
  </AbsoluteFill>;
}

export function BRollOverlayLayer(p: Props) {
  return <>{p.b_roll.map((item,i)=><Timed span={item} key={i}><InsertClip item={item} job={p.jobId}/></Timed>)}</>;
}

function Card({item}: {item:Callout}) {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const progress = spring({frame,fps,config:{damping:17,stiffness:160}});
  return <div style={{position:'absolute',top:'12%',left:'7%',right:'7%',textAlign:'center',
    opacity:Math.min(progress,1),transform:'translateY('+((1-progress)*48)+'px) scale('+(0.94+0.06*progress)+')'}}>
    <div style={{color:'#fff',fontSize:60,lineHeight:1.12,fontWeight:750,
      overflowWrap:'anywhere',textShadow:'0 2px 5px #000, 0 4px 12px #000b'}}>
      {item.title}{item.subtitle && <div style={{fontSize:35,fontWeight:450,marginTop:10}}>{item.subtitle}</div>}
    </div></div>;
}

export function TitleCalloutLayer(p: Props) {
  return <>{p.callouts.map((item,i)=><Timed span={item} key={i}><Card item={item}/></Timed>)}</>;
}

export function KaraokeSubtitlesLayer(p: Props) {
  const t = useCurrentFrame()/useVideoConfig().fps;
  const groups = React.useMemo(() => {
    const result: Word[][] = [];
    let group: Word[] = [];
    for (const word of p.words) {
      const last = group.at(-1);
      if (last && (group.length >= 4 || word.startSec - last.endSec > 0.35 ||
          word.endSec - group[0].startSec > 2 || /[.!?…]$/.test(last.word))) {
        result.push(group);
        group = [];
      }
      group.push(word);
    }
    if (group.length) result.push(group);
    return result;
  }, [p.words]);
  const group = groups.find(g => t >= g[0].startSec && t < g[g.length - 1].endSec);
  if (!group || !group.some(w => t >= w.startSec && t < w.endSec + 0.18)) return null;
  return <div style={{position:'absolute',bottom:'16%',width:'100%',padding:'0 7%',boxSizing:'border-box',textAlign:'center'}}>
    <div style={{display:'inline-block',background:'#101816da',color:'white',borderRadius:18,
      padding:'12px 22px',fontSize:51,fontWeight:700,lineHeight:1.24,textShadow:'0 2px 3px black'}}>
      {group.map((w,i)=><React.Fragment key={i}>{i>0?' ':''}
        <span style={{color:t >= w.startSec && t < w.endSec && p.subtitle_style==='highlight'?p.accent:'white'}}>{w.word}</span>
      </React.Fragment>)}
    </div></div>;
}

export function AudioLayer(p: Props) {
  const frame = useCurrentFrame();
  const {fps,durationInFrames} = useVideoConfig();
  const gain = Math.min(1,(durationInFrames-1-frame)/Math.max(1,Math.min(fps*1.5,durationInFrames-1)));
  return p.music ? <Audio src={media(p.jobId,p.music)} loop volume={p.volume*gain}/> : null;
}

function MainComposition(p: Props) {
  return <AbsoluteFill style={{background:'#101816',fontFamily:'DejaVu Sans, sans-serif'}}>
    <BackgroundLayer cues={p.backgrounds ?? []} jobId={p.jobId}/>
    <BaseAvatarLayer {...p}/><BRollOverlayLayer {...p}/><TitleCalloutLayer {...p}/>
    <KaraokeSubtitlesLayer {...p}/><AudioLayer {...p}/>
  </AbsoluteFill>;
}

const defaults:Props = {jobId:'demo',avatar:'avatar.mp4',zoom_events:[],callouts:[],words:[],
  b_roll:[],music:null,volume:0.12,accent:'#CBFF68',subtitle_style:'highlight'};
export function Studio() {
  return <Composition id="MainComposition" component={MainComposition} fps={30} width={1080}
    height={1920} durationInFrames={30} defaultProps={defaults} calculateMetadata={async ({props})=>{
      const {durationInSeconds, width, height} = await getVideoMetadata(media(props.jobId,props.avatar));
      const landscape = width > height;
      return {width: landscape ? 1920 : 1080, height: landscape ? 1080 : 1920,
        durationInFrames:Math.max(1,Math.ceil(durationInSeconds*30))};
    }}/>;
}
