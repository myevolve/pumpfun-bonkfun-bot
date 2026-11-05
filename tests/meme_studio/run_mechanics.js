"use strict";

const mechanics = require(process.argv[2]);
const fn = process.argv[3];
const payload = JSON.parse(process.argv[4]);
let result;
if (fn === "create") {
  result = mechanics.createState(payload);
} else if (fn === "step") {
  result = mechanics.step(payload.state, payload.input, payload.dt);
} else if (fn === "share") {
  result = { text: mechanics.shareText(payload.score, payload.title, payload.suffix) };
} else {
  throw new Error("unknown fn " + fn);
}
process.stdout.write(JSON.stringify(result));
