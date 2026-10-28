"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.MemeMechanics = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const GRAVITY = 2200;
  const JUMP_V = 700;
  const RUN_SPEED = 260;
  const MOVE_SPEED = 220;
  const COMBO_WINDOW = 0.8;

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function createState(spec) {
    const template = spec.template_id;
    const seed = spec.seed || 1;
    if (template === "endless_runner") {
      return {
        template: template,
        t: 0,
        y: 0,
        vy: 0,
        score: 0,
        alive: true,
        player_x: 64,
        player_w: 28,
        player_h: 36,
        obstacles: [{ x: 900, w: 28, h: 40 }],
        rand: seed,
      };
    }
    if (template === "tap_clicker") {
      return {
        template: template,
        t: 0,
        score: 0,
        combo: 0,
        last_tap: -10,
        alive: true,
        pulse: 0,
      };
    }
    if (template === "dodge_catch") {
      return {
        template: template,
        t: 0,
        score: 0,
        alive: true,
        player_x: 160,
        player_y: 520,
        player_w: 48,
        player_h: 28,
        width: 360,
        items: [
          { x: 40, y: 0, w: 16, h: 16, kind: "good", vy: 140 },
          { x: 280, y: 30, w: 16, h: 16, kind: "bad", vy: 160 },
        ],
        rand: seed || 1,
        spawned: 0,
      };
    }
    throw new Error("unknown template");
  }

  function shareText(score, title, suffix) {
    return "I scored " + score + " in " + title + ". " + suffix;
  }

  function nextRand(state) {
    let value = state.rand >>> 0;
    value = (Math.imul(value, 1664525) + 1013904223) >>> 0;
    state.rand = value;
    return value / 4294967296;
  }

  function overlaps(a, b) {
    return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  }

  function step(state, input, dt) {
    const next = clone(state);
    const controls = input || {};
    const frame = Math.max(0, Number(dt) || 0);
    if (!next.alive) {
      return next;
    }
    if (next.template === "endless_runner") {
      return stepRunner(next, controls, frame);
    }
    if (next.template === "tap_clicker") {
      return stepClicker(next, controls, frame);
    }
    return stepDodge(next, controls, frame);
  }

  function stepRunner(state, input, dt) {
    // gravity pulls the runner down; a jump leaves the ground
    if (input.jump && state.y <= 0.5) {
      state.vy = JUMP_V;
    }
    state.vy -= GRAVITY * dt;
    state.y += state.vy * dt;
    if (state.y <= 0) {
      state.y = 0;
      state.vy = 0;
    }
    const obstacles = state.obstacles || [];
    for (let i = 0; i < obstacles.length; i += 1) {
      obstacles[i].x -= RUN_SPEED * dt;
      const player = { x: state.player_x, y: 0, w: state.player_w, h: state.player_h };
      const obs = obstacles[i];
      if (state.y < obs.h && overlaps(player, { x: obs.x, y: 0, w: obs.w, h: state.player_h })) {
        state.alive = false;
      }
    }
    state.obstacles = obstacles.filter(function (obs) {
      return obs.x > -40;
    });
    const last = state.obstacles[state.obstacles.length - 1];
    if (!last || last.x < 500) {
      state.obstacles.push({ x: 900, w: 28, h: 40 });
    }
    if (state.alive) {
      state.score += dt * 10;
    }
    state.t += dt;
    return state;
  }

  function stepClicker(state, input, dt) {
    const t = state.t + dt;
    if (input.tap) {
      if (t - state.last_tap > COMBO_WINDOW) {
        state.combo = 0;
      }
      const points = 1 + Math.min(state.combo, 9);
      state.score += points;
      state.combo += 1;
      state.last_tap = t;
      state.pulse = 1;
    } else if (t - state.last_tap > COMBO_WINDOW) {
      state.combo = 0;
      state.pulse = Math.max(0, state.pulse - dt);
    } else {
      state.pulse = Math.max(0, state.pulse - dt);
    }
    state.t = t;
    return state;
  }

  function stepDodge(state, input, dt) {
    // catch good items and dodge bad ones
    if (input.left) {
      state.player_x -= MOVE_SPEED * dt;
    }
    if (input.right) {
      state.player_x += MOVE_SPEED * dt;
    }
    if (state.player_x < 0) {
      state.player_x = 0;
    }
    const maxX = state.width - state.player_w;
    if (state.player_x > maxX) {
      state.player_x = maxX;
    }
    const kept = [];
    const items = state.items || [];
    for (let i = 0; i < items.length; i += 1) {
      const moved = clone(items[i]);
      moved.y += (moved.vy || 0) * dt;
      const player = {
        x: state.player_x,
        y: state.player_y,
        w: state.player_w,
        h: state.player_h,
      };
      if (overlaps(player, moved)) {
        if (moved.kind === "bad") {
          state.alive = false;
        } else {
          state.score += 1;
        }
        continue;
      }
      if (moved.y < 700) {
        kept.push(moved);
      }
    }
    state.items = kept;
    if (state.alive) {
      while (state.items.length < 2) {
        const lane = nextRand(state) < 0.5 ? 12 : state.width - 36;
        const kind = nextRand(state) < 0.45 ? "bad" : "good";
        state.items.push({ x: lane, y: -24, w: 16, h: 16, kind: kind, vy: 420 });
        state.spawned = (state.spawned || 0) + 1;
      }
    }
    state.t += dt;
    return state;
  }

  function drawCreature(ctx, x, y, spec) {
    const palette = spec.palette || {};
    ctx.fillStyle = palette.primary || "#f2c14e";
    ctx.beginPath();
    ctx.arc(x + 32, y + 32, 28, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = palette.ink || "#1b1f3b";
    ctx.fillRect(x + 18, y + 24, 6, 6);
    ctx.fillRect(x + 36, y + 24, 6, 6);
  }

  function draw(ctx, state, spec, images) {
    const w = ctx.canvas.width;
    const h = ctx.canvas.height;
    const palette = spec.palette || {};
    ctx.fillStyle = palette.background || "#1b1f3b";
    ctx.fillRect(0, 0, w, h);
    if (images && images.background && images.background.complete && images.background.naturalWidth) {
      ctx.drawImage(images.background, 0, 0, w, h);
    }
    let x = 48;
    let y = h - 96;
    if (state.template === "endless_runner") {
      x = state.player_x;
      y = h - 96 - state.y;
    } else if (state.template === "dodge_catch") {
      x = state.player_x;
      y = Math.min(h - 64, state.player_y);
    } else if (state.pulse) {
      y -= 8;
    }
    if (images && images.sprite && images.sprite.complete && images.sprite.naturalWidth) {
      ctx.drawImage(images.sprite, x, y, 64, 64);
    } else {
      drawCreature(ctx, x, y, spec);
    }
    ctx.fillStyle = palette.secondary || "#e07a5f";
    if (state.template === "endless_runner") {
      (state.obstacles || []).forEach(function (obs) {
        ctx.fillRect(obs.x, h - obs.h - 40, obs.w, obs.h);
      });
    }
    if (state.template === "dodge_catch") {
      (state.items || []).forEach(function (item) {
        ctx.fillStyle = item.kind === "bad" ? palette.secondary : palette.accent;
        ctx.fillRect(item.x, item.y, item.w, item.h);
      });
    }
  }

  return {
    createState: createState,
    step: step,
    draw: draw,
    drawCreature: drawCreature,
    shareText: shareText,
  };
});
