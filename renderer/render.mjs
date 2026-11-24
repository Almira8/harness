import { readFile } from "node:fs/promises";
import path from "node:path";
import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";

const [, , propsPath, outputLocation] = process.argv;
if (!propsPath || !outputLocation) {
  throw new Error("Provide props JSON and output MP4 paths");
}
const inputProps = JSON.parse(await readFile(propsPath, "utf8"));
const serveUrl = await bundle({ entryPoint: path.resolve("src/index.tsx") });
const composition = await selectComposition({
  serveUrl,
  id: "Reel",
  inputProps,
});
await renderMedia({
  serveUrl,
  composition,
  inputProps,
  codec: "h264",
  outputLocation,
  concurrency: 2,
  crf: 20,
  audioCodec: "aac",
});
