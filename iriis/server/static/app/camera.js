/* Seryn camera screen: a video-call style view where the AI watches through the phone camera and you just talk.
 * Shared by the Iriis app (through Iriis: /v1/camera, /v1/bench/*) and the Seryn Vision page (/ws, /api/*).
 *
 *   SerynCamera.open({ ws, api, media, stt, files, confirm, title, onClose })  ->  { close, ask }
 *
 * Protocol (same as Seryn Vision /ws): binary JPEG frames up (next one only after "frame_ack"), JSON messages both
 * ways: ask, goal, procedure, step, coach, recent, ping, rtc / hello, state, perception, thinking, answer, alert, busy,
 * coach, job, video_report, relay, viewer_join, viewer_leave, rtc, error. Through Iriis also: brain_thinking, brain,
 * brain_done, brain_confirm (answered with POST opts.confirm).
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------ icons (24px, stroke = currentColor)
  const P = {
    down: '<path d="M6 9l6 6 6-6"/>',
    flip: '<path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 21v-5h5"/>',
    mic: '<rect x="9" y="2.5" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3.5"/>',
    micoff: '<path d="M15 9.3V5.5a3 3 0 0 0-5.7-1.3"/><path d="M9 9v2.5a3 3 0 0 0 5 2.2"/><path d="M19 11a7 7 0 0 1-1.1 3.8M5 11a7 7 0 0 0 11.3 5.5"/><path d="M12 18v3.5"/><path d="M3 3l18 18"/>',
    compass: '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
    more: '<circle cx="5.5" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="18.5" cy="12" r="1.6" fill="currentColor" stroke="none"/>',
    end: '<path fill="currentColor" stroke="none" d="M12 8.6c-3.4 0-6.6 1-9 2.9-.6.5-.8 1.3-.5 2l1 2.1c.3.7 1.1 1 1.8.8l3-.9c.6-.2 1-.7 1-1.3v-1.9c1.7-.5 3.7-.5 5.4 0v1.9c0 .6.4 1.1 1 1.3l3 .9c.7.2 1.5-.1 1.8-.8l1-2.1c.3-.7.1-1.5-.5-2-2.4-1.9-5.6-2.9-9-2.9z"/>',
    torch: '<path d="M13 2L4.5 13.5H11l-1 8.5 8.5-11.5H12z"/>',
    history: '<path d="M3 12a9 9 0 1 0 2.7-6.4L3 8.2"/><path d="M3 3.5v4.7h4.7"/><path d="M12 7.5V12l3 2"/>',
    target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1" fill="currentColor"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6h.01M4 12h.01M4 18h.01" stroke-width="3"/>',
    chat: '<path d="M21 14.5a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M8 8.5h8M8 12h5"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    camera: '<path d="M22 18a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8.5a2 2 0 0 1 2-2h3.5l2-3h5l2 3H20a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="3.8"/>',
    speaker: '<path d="M11 5L6 9H2.5v6H6l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9.5 9.5 0 0 1 0 13"/>',
    sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/><path d="M1.5 14h5M9.5 8h5M17.5 16h5"/>',
    film: '<rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M7 3v18M17 3v18M3 7.5h4M3 12h4M3 16.5h4M17 7.5h4M17 12h4M17 16.5h4"/>',
    eye: '<path d="M1.5 12S5.5 4.5 12 4.5 22.5 12 22.5 12 18.5 19.5 12 19.5 1.5 12 1.5 12z"/><circle cx="12" cy="12" r="3"/>',
    check: '<path d="M4.5 12.5l5 5 10-11"/>',
    left: '<path d="M15 18l-6-6 6-6"/>',
    right: '<path d="M9 18l6-6-6-6"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    warn: '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    stop: '<path d="M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9z"/><path d="M12 8v4M12 16h.01"/>',
    spark: '<path d="M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9z"/>',
    upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
  };
  const I = (n, cls) => `<svg class="sc-i${cls ? " " + cls : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[n]}</svg>`;

  const CSS = `
  .sc-root{--glass:rgba(24,24,28,.58);--glass2:rgba(24,24,28,.78);--line:rgba(255,255,255,.1);--mute:#a1a1aa;--green:#22c55e;--red:#ef4444;--amber:#f59e0b;
    position:fixed;inset:0;z-index:50;background:#000;color:#fff;font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    overflow:hidden;-webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}
  .sc-root *{box-sizing:border-box}
  .sc-root :where(button){font:inherit;color:inherit;border:none;background:none;cursor:pointer;padding:0}
  .sc-root input,.sc-root select{-webkit-user-select:text;user-select:text}
  .sc-i{width:22px;height:22px;flex:0 0 auto}
  .sc-glass{background:var(--glass);-webkit-backdrop-filter:blur(18px) saturate(150%);backdrop-filter:blur(18px) saturate(150%)}
  .sc-video,.sc-canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
  .sc-scrim{position:absolute;left:0;right:0;pointer-events:none}
  .sc-scrim.t{top:0;height:170px;background:linear-gradient(rgba(0,0,0,.62),rgba(0,0,0,.25) 55%,transparent)}
  .sc-scrim.b{bottom:0;height:340px;background:linear-gradient(transparent,rgba(0,0,0,.35) 40%,rgba(0,0,0,.7))}

  .sc-topstack{position:absolute;top:0;left:0;right:0;z-index:2;display:flex;flex-direction:column;gap:8px;pointer-events:none}
  .sc-topstack>*{pointer-events:auto}
  .sc-top{display:flex;align-items:center;gap:6px;padding:8px 10px 0;padding-top:max(8px,env(safe-area-inset-top))}
  .sc-ib{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;flex:0 0 auto;transition:background .15s}
  .sc-ib:active{background:rgba(255,255,255,.14)}
  .sc-ib .sc-i{width:26px;height:26px}
  .sc-ib.sm .sc-i{width:20px;height:20px}
  .sc-ib.on{background:rgba(255,255,255,.9);color:#111}
  .sc-title{flex:1;min-width:0;padding-left:2px}
  .sc-title b{display:block;font-size:17px;font-weight:600;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sc-sub{display:block;font-size:13px;color:rgba(255,255,255,.78);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}
  .sc-chips{display:flex;gap:6px;align-items:center;flex:0 0 auto}
  .sc-tag{display:none;align-items:center;gap:6px;height:26px;padding:0 10px;border-radius:99px;font-size:12px;font-weight:600;
    font-variant-numeric:tabular-nums;white-space:nowrap;animation:scpop .2s ease-out}
  .sc-tag.show{display:inline-flex}
  .sc-tag.rec{background:var(--red)}
  .sc-tag.rec i{width:7px;height:7px;border-radius:50%;background:#fff;animation:scblink 1s steps(2) infinite}
  .sc-tag.coach{background:rgba(34,197,94,.2);color:#bbf7d0;border:1px solid rgba(34,197,94,.45)}
  .sc-tag.coach i{width:7px;height:7px;border-radius:50%;background:var(--green);animation:scpulse 1.6s ease-out infinite}
  .sc-tag.view{background:var(--glass)}
  .sc-tag .sc-i{width:14px;height:14px}

  .sc-step{display:none;width:min(calc(100% - 20px),600px);margin:0 auto;border-radius:18px;overflow:hidden;border:1px solid var(--line);animation:scdown .22s ease-out}
  .sc-step.show{display:block}
  .sc-step .hd{display:flex;align-items:center;gap:8px;padding:10px 6px 0 14px}
  .sc-step .k{flex:1;font-size:11.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#86efac}
  .sc-step .k.done{color:#86efac}
  .sc-step .ctl{display:flex;gap:2px}
  .sc-step .ctl button{width:36px;height:36px;border-radius:50%;display:grid;place-items:center}
  .sc-step .ctl button:active{background:rgba(255,255,255,.14)}
  .sc-step .ctl .sc-i{width:19px;height:19px}
  .sc-step .tx{padding:2px 14px 12px;font-size:15px;font-weight:500;line-height:1.35}
  .sc-pb{height:3px;background:rgba(255,255,255,.12)}
  .sc-pb i{display:block;height:100%;width:0;background:var(--green);transition:width .3s ease}

  .sc-reading{display:none;align-self:flex-end;margin-right:10px;padding:6px 14px;border-radius:99px;border:1px solid var(--line);
    font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
  .sc-reading.show{display:block;animation:scpop .2s ease-out}
  .sc-reading.out{color:#fcd34d;border-color:rgba(245,158,11,.55)}

  .sc-banner{position:absolute;z-index:6;left:10px;right:10px;top:max(8px,env(safe-area-inset-top));margin:0 auto;max-width:560px;
    display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border-radius:18px;box-shadow:0 10px 30px rgba(0,0,0,.4);
    transform:translateY(calc(-100% - 30px));opacity:0;transition:transform .25s cubic-bezier(.2,.8,.2,1),opacity .2s;pointer-events:none}
  .sc-banner.show{transform:none;opacity:1;pointer-events:auto}
  .sc-banner.warning{background:#b45309}.sc-banner.danger{background:#b91c1c}
  .sc-banner .sc-i{width:24px;height:24px;margin-top:1px}
  .sc-banner b{display:block;font-size:15px}.sc-banner span{display:block;font-size:14px;opacity:.95;-webkit-user-select:text;user-select:text}

  .sc-botstack{position:absolute;z-index:2;bottom:0;left:50%;transform:translateX(-50%);width:min(100%,600px);display:flex;flex-direction:column;gap:8px;
    padding:0 8px max(10px,env(safe-area-inset-bottom))}
  .sc-toast{position:absolute;bottom:calc(100% + 10px);left:50%;transform:translate(-50%,8px);opacity:0;pointer-events:none;max-width:min(calc(100% - 32px),340px);
    width:max-content;padding:9px 16px;border-radius:99px;background:var(--glass2);-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);
    font-size:13.5px;font-weight:500;text-align:center;transition:opacity .2s,transform .2s;display:flex;align-items:center;gap:8px}
  .sc-toast.show{opacity:1;transform:translate(-50%,0)}
  .sc-toast.ok .dot{background:var(--green)}
  .sc-toast .dot{width:8px;height:8px;border-radius:50%;background:#a1a1aa;flex:0 0 auto}
  .sc-toast.coach .dot{background:var(--green);animation:scpulse 1.6s ease-out infinite}

  .sc-caps{display:flex;flex-direction:column;gap:6px;padding:28px 4px 0;max-height:38vh;overflow:hidden;justify-content:flex-end;
    -webkit-mask-image:linear-gradient(transparent,#000 30px);mask-image:linear-gradient(transparent,#000 30px)}
  .sc-cap{max-width:min(86%,500px);padding:9px 13px;border-radius:18px;align-self:flex-start;border-bottom-left-radius:6px;
    background:var(--glass2);-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);animation:scin .22s ease-out;
    transition:opacity .25s;-webkit-user-select:text;user-select:text;font-size:15px;line-height:1.38;word-wrap:break-word}
  .sc-cap.me{align-self:flex-end;background:rgba(255,255,255,.88);color:#111;border-radius:18px;border-bottom-right-radius:6px}
  .sc-cap.dim{color:rgba(255,255,255,.66)}.sc-cap.me.dim{color:rgba(17,17,17,.6);background:rgba(255,255,255,.72)}
  .sc-cap.dim .sc-fl,.sc-cap.dim .lnk{opacity:.8}
  .sc-cap small{display:block;color:var(--mute);font-size:11.5px;margin-top:3px}
  .sc-cap .lnk{display:inline-flex;align-items:center;gap:4px;margin-top:6px;font-size:13px;font-weight:600;color:#86efac}
  .sc-dots{display:inline-flex;gap:4px;padding:5px 2px}
  .sc-dots span{width:7px;height:7px;border-radius:50%;background:#fff;animation:scb 1.2s infinite ease-in-out}
  .sc-dots span:nth-child(2){animation-delay:.15s}.sc-dots span:nth-child(3){animation-delay:.3s}
  .sc-fl{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .sc-file{display:inline-flex;align-items:center;gap:10px;max-width:100%;padding:6px 12px 6px 6px;border-radius:14px;background:rgba(255,255,255,.1);
    color:#fff;text-decoration:none;border:1px solid var(--line);transition:background .15s}
  .sc-file:active{background:rgba(255,255,255,.18)}
  .sc-file .fi{width:36px;height:36px;border-radius:9px;display:grid;place-items:center;font-size:10px;font-weight:800;letter-spacing:.03em;background:#3f3f46;flex:0 0 auto}
  .sc-file .fi.pdf{background:#dc2626}.sc-file .fi.doc{background:#2563eb}.sc-file .fi.xls{background:#16a34a}
  .sc-file img{width:52px;height:52px;border-radius:10px;object-fit:cover;background:#27272a;flex:0 0 auto}
  .sc-file .fn{min-width:0}.sc-file b{display:block;font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px}
  .sc-file small{margin:0;font-size:11.5px}

  .sc-work{display:none;align-self:flex-start;align-items:center;gap:9px;max-width:100%;margin-left:4px;padding:7px 14px 7px 10px;border-radius:99px;
    font-size:13px;color:#e4e4e7;border:1px solid var(--line);animation:scin .2s ease-out}
  .sc-work.show{display:inline-flex}
  .sc-work span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sc-spin{width:14px;height:14px;border-radius:50%;border:2px solid rgba(255,255,255,.25);border-top-color:#fff;animation:scspin .8s linear infinite;flex:0 0 auto}

  .sc-cf{padding:14px;border-radius:22px;background:var(--glass2);-webkit-backdrop-filter:blur(20px) saturate(150%);backdrop-filter:blur(20px) saturate(150%);
    border:1px solid rgba(255,255,255,.14);box-shadow:0 12px 32px rgba(0,0,0,.35);animation:scin .22s ease-out}
  .sc-cf .lb{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;color:#86efac;letter-spacing:.02em;margin-bottom:4px}
  .sc-cf .lb .sc-i{width:15px;height:15px}
  .sc-cf .q{font-size:16px;font-weight:600;line-height:1.35;margin-bottom:12px;-webkit-user-select:text;user-select:text}
  .sc-cf .bt{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .sc-cf button{height:44px;border-radius:14px;background:rgba(255,255,255,.12);font-weight:600;transition:transform .12s,background .15s}
  .sc-cf button:active{transform:scale(.97)}
  .sc-cf button.y{background:var(--green);color:#052e16}
  .sc-cf button.all{grid-column:1/-1;height:38px;background:transparent;border:1px solid var(--line);font-size:13.5px;color:#d4d4d8}
  .sc-cf.busy{opacity:.6;pointer-events:none}

  .sc-bar{display:flex;justify-content:space-between;align-items:flex-start;padding:10px 8px 8px;border-radius:30px;border:1px solid var(--line)}
  .sc-b{display:flex;flex-direction:column;align-items:center;gap:5px;min-width:48px;font-size:10.5px;font-weight:500;color:rgba(255,255,255,.78);
    font-variant-numeric:tabular-nums}
  .sc-b .c{position:relative;width:48px;height:48px;border-radius:50%;display:grid;place-items:center;background:rgba(255,255,255,.13);
    transition:background .2s,color .2s,transform .15s,box-shadow .08s linear}
  .sc-b:active .c{transform:scale(.92)}
  .sc-b.mic .c{box-shadow:0 0 0 calc(var(--lvl,0) * 7px) rgba(255,255,255,.22)}
  .sc-b.mic.off .c{background:#fff;color:#111;box-shadow:none}
  .sc-b.coach.on .c{background:var(--green);color:#052e16}
  .sc-b.end .c{background:var(--red)}
  .sc-b .rd{width:18px;height:18px;border-radius:50%;background:var(--red);transition:border-radius .2s,width .2s,height .2s}
  .sc-b.rec .c{background:#fff}
  .sc-b.rec .rd{width:16px;height:16px;border-radius:4px}
  .sc-b.rec .c::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:2px solid rgba(239,68,68,.7);animation:scpulse2 1.4s ease-out infinite}
  .sc-b.rec{color:#fca5a5}

  .sc-dim{position:absolute;inset:0;z-index:7;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .22s}
  .sc-dim.open{opacity:1;pointer-events:auto}
  .sc-sheet{position:absolute;z-index:8;left:50%;bottom:0;width:min(100%,560px);max-height:86%;display:flex;flex-direction:column;
    background:#17171b;border-radius:24px 24px 0 0;border:1px solid rgba(255,255,255,.06);border-bottom:none;box-shadow:0 -10px 40px rgba(0,0,0,.45);
    transform:translate(-50%,calc(100% + 20px));transition:transform .26s cubic-bezier(.2,.85,.25,1);padding-bottom:env(safe-area-inset-bottom)}
  .sc-sheet.open{transform:translate(-50%,0)}
  .sc-sheet.drag{transition:none}
  .sc-shh{flex:0 0 auto;padding:8px 8px 4px 20px;touch-action:none;cursor:grab}
  .sc-grab{width:38px;height:5px;border-radius:9px;background:#3f3f46;margin:0 auto 6px}
  .sc-shh .r{display:flex;align-items:center}
  .sc-shh h3{flex:1;margin:0;font-size:17px;font-weight:650;letter-spacing:-.01em}
  .sc-shb{overflow-y:auto;overscroll-behavior:contain;padding:2px 8px 14px}
  .sc-sheet[data-pane="more"] .sc-pane.report,.sc-sheet[data-pane="report"] .sc-pane.more{display:none}
  .sc-item{position:relative}
  .sc-item+.sc-item::before{content:"";position:absolute;top:0;left:66px;right:10px;height:1px;background:rgba(255,255,255,.06)}
  .sc-item.hide{display:none}
  .sc-row{display:flex;align-items:center;gap:14px;width:100%;padding:11px 10px;border-radius:14px;text-align:left;transition:background .15s}
  .sc-row:active{background:rgba(255,255,255,.05)}
  .sc-ic{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;flex:0 0 auto;color:var(--t);
    background:rgba(255,255,255,.07);background:color-mix(in srgb,var(--t) 17%,transparent)}
  .sc-rt{flex:1;min-width:0}
  .sc-rt b{display:block;font-size:15px;font-weight:600}
  .sc-rt small{display:block;color:var(--mute);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sc-chev{color:#52525b;transition:transform .2s}
  .sc-item.open .sc-chev{transform:rotate(90deg)}
  .sc-x{display:grid;grid-template-rows:0fr;transition:grid-template-rows .22s ease}
  .sc-x>div{overflow:hidden;min-height:0}
  .sc-item.open .sc-x{grid-template-rows:1fr}
  .sc-xi{padding:0 10px 14px 66px;display:flex;flex-direction:column;gap:8px}
  .sc-f{display:flex;gap:8px}
  .sc-in{flex:1;min-width:0;height:44px;padding:0 14px;border-radius:12px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.06);
    color:#fff;font:inherit;font-size:15px;outline:none;transition:border-color .15s}
  .sc-in:focus{border-color:rgba(34,197,94,.8)}
  select.sc-in{appearance:none;-webkit-appearance:none;padding-right:34px;
    background:rgba(255,255,255,.06) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23a1a1aa' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E") no-repeat right 10px center/18px}
  select.sc-in option{background:#17171b}
  .sc-bt{flex:0 0 auto;height:44px;padding:0 16px;border-radius:12px;background:rgba(255,255,255,.1);font-weight:600;font-size:14.5px;
    display:inline-flex;align-items:center;justify-content:center;gap:6px;transition:background .15s,transform .12s}
  .sc-bt:active{transform:scale(.97)}
  .sc-bt.p{background:var(--green);color:#052e16}
  .sc-bt .sc-i{width:18px;height:18px}
  .sc-filepick{position:relative;overflow:hidden;flex:1;justify-content:flex-start;font-weight:500;color:#d4d4d8;min-width:0}
  .sc-filepick span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sc-filepick input{position:absolute;inset:0;opacity:0;cursor:pointer}
  .sc-run{display:none;flex-direction:column;gap:8px;padding:12px;border-radius:14px;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.25)}
  .sc-run.show{display:flex}
  .sc-run .n{font-size:13px;color:#86efac;font-weight:600}
  .sc-run .s{font-size:14px}
  .sc-run .sc-pb{border-radius:9px;overflow:hidden}
  .sc-run .sc-f>*{flex:1}
  .sc-hint{font-size:12px;color:#71717a}
  .sc-sw{width:46px;height:28px;border-radius:99px;background:#3f3f46;position:relative;flex:0 0 auto;transition:background .2s}
  .sc-sw::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:transform .2s}
  .sc-sw.on{background:var(--green)}.sc-sw.on::after{transform:translateX(18px)}
  .sc-row select.sc-in{flex:0 0 auto;width:96px;height:36px;font-size:14px}

  .sc-rp{padding:4px 12px 8px}
  .sc-prog{display:flex;gap:12px;align-items:center;padding:14px;border-radius:16px;background:rgba(255,255,255,.05);border:1px solid var(--line)}
  .sc-prog .sc-ic{--t:#a78bfa}
  .sc-prog .w{flex:1;min-width:0}
  .sc-prog b{display:block;font-size:15px}
  .sc-prog small{display:block;color:var(--mute);font-size:13px;margin-bottom:8px}
  .sc-prog .sc-pb{border-radius:9px;overflow:hidden;height:4px}
  .sc-prog .sc-pb.ind i{width:35%;animation:scind 1.3s ease-in-out infinite}
  .sc-prog.done .sc-pb{display:none}.sc-prog.done small{margin:0}
  .sc-prog.fail .sc-ic{--t:#f87171}
  .sc-rb{-webkit-user-select:text;user-select:text}
  .sc-rb .sum{font-size:16px;line-height:1.5;margin:16px 2px 6px}
  .sc-rb .ans{margin:10px 0;padding:12px 14px;border-radius:14px;background:rgba(34,197,94,.09);border:1px solid rgba(34,197,94,.22);font-size:15px}
  .sc-rb .meta{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 4px}
  .sc-rb .meta span{font-size:12px;padding:3px 9px;border-radius:99px;background:rgba(255,255,255,.07);color:#d4d4d8}
  .sc-rb .prob{display:flex;gap:10px;align-items:flex-start;margin:8px 0;padding:10px 12px;border-radius:12px;background:rgba(245,158,11,.1);
    border:1px solid rgba(245,158,11,.3);color:#fde68a;font-size:14px}
  .sc-rb .prob .sc-i{width:18px;height:18px;margin-top:1px}
  .sc-rb h5{margin:18px 2px 8px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#71717a}
  .sc-tl{list-style:none;margin:0;padding:0 0 0 2px}
  .sc-tl li{position:relative;display:flex;gap:12px;padding:0 0 14px 22px}
  .sc-tl li::before{content:"";position:absolute;left:4px;top:7px;width:10px;height:10px;border-radius:50%;background:#60a5fa;box-shadow:0 0 0 3px #17171b}
  .sc-tl li::after{content:"";position:absolute;left:8.5px;top:19px;bottom:-1px;width:1px;background:rgba(255,255,255,.12)}
  .sc-tl li:last-child::after{display:none}
  .sc-tl li.warning::before{background:var(--amber)}.sc-tl li.danger::before{background:var(--red)}.sc-tl li.ok::before{background:var(--green)}
  .sc-tl time{flex:0 0 40px;font-size:13px;font-weight:600;color:#a1a1aa;font-variant-numeric:tabular-nums;padding-top:1px}
  .sc-tl span{font-size:14.5px;line-height:1.4}

  .sc-msg{position:absolute;inset:0;z-index:9;display:none;place-items:center;text-align:center;padding:28px;background:rgba(0,0,0,.86)}
  .sc-msg>div{max-width:380px;display:flex;flex-direction:column;align-items:center;gap:14px}
  .sc-msg .sc-ic{--t:#fbbf24;width:56px;height:56px;border-radius:18px}
  .sc-msg .sc-msgtext{font-size:16px;line-height:1.45}
  .sc-msg .r{display:flex;gap:8px}

  @keyframes scin{from{transform:translateY(8px);opacity:0}}
  @keyframes scdown{from{transform:translateY(-8px);opacity:0}}
  @keyframes scpop{from{transform:scale(.85);opacity:0}}
  @keyframes scb{0%,60%,100%{transform:none;opacity:.45}30%{transform:translateY(-4px);opacity:1}}
  @keyframes scspin{to{transform:rotate(360deg)}}
  @keyframes scblink{50%{opacity:0}}
  @keyframes scpulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,.7)}100%{box-shadow:0 0 0 8px rgba(34,197,94,0)}}
  @keyframes scpulse2{0%{transform:scale(.95);opacity:1}100%{transform:scale(1.25);opacity:0}}
  @keyframes scind{0%{transform:translateX(-100%)}100%{transform:translateX(300%)}}
  @media (max-width:400px){.sc-wide{display:none}}
  @media (min-width:420px){.sc-b .c{width:54px;height:54px}.sc-bar{padding:10px 14px 8px}}
  @media (prefers-reduced-motion:reduce){.sc-root *,.sc-root *::before,.sc-root *::after{animation-duration:1ms!important;animation-iteration-count:1!important;transition-duration:1ms!important}}
  `;

  const $ = (root, sel) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const mmss = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const row = (k, icon, tint, title, desc, body) => `
    <div class="sc-item" data-k="${k}">
      <button class="sc-row" data-a="${body ? "x:" + k : k}"><span class="sc-ic" style="--t:${tint}">${I(icon)}</span>
        <span class="sc-rt"><b>${title}</b><small class="sc-d-${k}">${desc}</small></span>${I("right", "sc-chev")}</button>
      ${body ? `<div class="sc-x"><div><div class="sc-xi">${body}</div></div></div>` : ""}
    </div>`;

  function open(opts) {
    const o = Object.assign({ ws: "/ws", api: "/api", media: "/media", stt: "/api/stt", files: "/v1/files", confirm: "/v1/confirm", title: "Camera", onClose: null }, opts || {});
    if (!document.getElementById("sc-style")) { const st = document.createElement("style"); st.id = "sc-style"; st.textContent = CSS; document.head.appendChild(st); }
    const root = document.createElement("div"); root.className = "sc-root";
    root.innerHTML = `
      <video class="sc-video" playsinline muted autoplay></video><canvas class="sc-canvas"></canvas>
      <div class="sc-scrim t"></div><div class="sc-scrim b"></div>
      <div class="sc-topstack">
        <div class="sc-top">
          <button class="sc-ib" data-a="close" aria-label="End and go back">${I("down")}</button>
          <div class="sc-title"><b>${esc(o.title)}</b><span class="sc-sub">connecting…</span></div>
          <div class="sc-chips">
            <span class="sc-tag rec"><i></i><span class="sc-rect">REC 0:00</span></span>
            <span class="sc-tag coach"><i></i>Coach<span class="sc-wide">on</span></span>
            <span class="sc-tag view" title="People watching">${I("eye")}<span class="sc-viewn">1</span></span>
          </div>
          <button class="sc-ib sm" data-a="torch" aria-label="Light" style="display:none">${I("torch")}</button>
        </div>
        <div class="sc-step sc-glass">
          <div class="hd"><span class="k sc-stepk">Step 1 of 1</span>
            <span class="ctl"><button data-a="prev" aria-label="Previous step">${I("left")}</button><button data-a="check" aria-label="Check this step">${I("check")}</button><button data-a="next" aria-label="Next step">${I("right")}</button></span></div>
          <div class="tx sc-steptext"></div>
          <div class="sc-pb"><i class="sc-stepbar"></i></div>
        </div>
        <div class="sc-reading sc-glass"></div>
      </div>
      <div class="sc-banner" data-a="banner"><span class="sc-bi"></span><div><b class="sc-bt1"></b><span class="sc-bt2"></span></div></div>
      <div class="sc-botstack">
        <div class="sc-toast"><span class="dot"></span><span class="sc-toastt"></span></div>
        <div class="sc-caps" aria-live="polite"></div>
        <div class="sc-work sc-glass"><span class="sc-spin"></span><span class="sc-workt"></span></div>
        <div class="sc-cfs"></div>
        <div class="sc-bar sc-glass">
          <button class="sc-b" data-a="flip" aria-label="Switch camera"><span class="c">${I("flip")}</span>Flip</button>
          <button class="sc-b mic" data-a="talk" aria-label="Mute microphone"><span class="c sc-micic">${I("mic")}</span><span class="sc-talklbl">Mute</span></button>
          <button class="sc-b recb" data-a="record" aria-label="Record a clip"><span class="c"><span class="rd"></span></span><span class="sc-reclbl">Record</span></button>
          <button class="sc-b coach" data-a="coach" aria-label="Coach mode"><span class="c">${I("compass")}</span>Coach</button>
          <button class="sc-b" data-a="more" aria-label="More options"><span class="c">${I("more")}</span>More</button>
          <button class="sc-b end" data-a="close" aria-label="End"><span class="c">${I("end")}</span>End</button>
        </div>
      </div>
      <div class="sc-dim" data-a="sheetclose"></div>
      <div class="sc-sheet" data-pane="more" role="dialog" aria-label="More options">
        <div class="sc-shh"><div class="sc-grab"></div><div class="r"><h3 class="sc-shtitle">More</h3>
          <button class="sc-ib sm" data-a="sheetclose" aria-label="Close">${I("x")}</button></div></div>
        <div class="sc-shb">
          <div class="sc-pane more">
            ${row("recent", "history", "#60a5fa", "What just happened?", "Review the last 90 seconds")}
            ${row("lastreport", "film", "#a78bfa", "Last report", "Open the latest review again")}
            ${row("goal", "target", "#22c55e", "Set a goal", "Tell it what you're doing, so Coach can help",
              `<div class="sc-f"><input class="sc-in sc-goal" placeholder="e.g. fill 12 °C packs, solder the heater…" enterkeyhint="done"><button class="sc-bt p" data-a="goal">Set</button></div>`)}
            ${row("proc", "list", "#f59e0b", "Step-by-step procedure", "Guide me through a saved procedure",
              `<div class="sc-run"><div class="n sc-runn"></div><div class="s sc-runs"></div><div class="sc-pb"><i class="sc-runbar"></i></div>
                 <div class="sc-f"><button class="sc-bt" data-a="prev">${I("left")}Prev</button><button class="sc-bt" data-a="check">${I("check")}Check</button><button class="sc-bt p" data-a="next">Next${I("right")}</button></div></div>
               <div class="sc-f"><select class="sc-in sc-procs"><option value="">Choose a procedure…</option></select><button class="sc-bt p" data-a="start">Start</button></div>
               <div class="sc-f"><input class="sc-in sc-seryn" placeholder="Import a Seryn protocol, e.g. P2"><button class="sc-bt" data-a="import">Import</button></div>`)}
            ${row("ask", "chat", "#38bdf8", "Ask by typing", "For noisy places, or when you can't talk",
              `<div class="sc-f"><input class="sc-in sc-q" placeholder="Ask about what the camera sees…" enterkeyhint="send"><button class="sc-bt p" data-a="ask">Ask</button></div>`)}
            ${row("teach", "book", "#f472b6", "Teach it", "Upload a manual or datasheet, or add a link",
              `<div class="sc-f"><label class="sc-bt sc-filepick">${I("upload")}<span class="sc-filesl">Choose files…</span><input class="sc-files" type="file" multiple accept=".pdf,.docx,.xlsx,.csv,.txt,.md,.json,.yaml,.yml,.html"></label><button class="sc-bt p" data-a="upload">Upload</button></div>
               <div class="sc-f"><input class="sc-in sc-url" placeholder="https://… a manual, datasheet or page" inputmode="url"><button class="sc-bt" data-a="addurl">Add</button></div>
               <div class="sc-hint">PDF, Word, Excel, CSV, text, Markdown, JSON, YAML or HTML</div>`)}
            ${row("snap", "camera", "#a3e635", "Take a snapshot", "Save a full-quality photo of this view")}
            <div class="sc-item"><button class="sc-row" data-a="mute"><span class="sc-ic" style="--t:#fb923c">${I("speaker")}</span>
              <span class="sc-rt"><b>Speak answers aloud</b><small>Turn off to read captions only</small></span><span class="sc-sw on sc-mutesw"></span></button></div>
            <div class="sc-item"><div class="sc-row"><span class="sc-ic" style="--t:#94a3b8">${I("sliders")}</span>
              <span class="sc-rt"><b>Video quality</b><small>Higher is sharper but uses more data</small></span>
              <select class="sc-in sc-res" aria-label="Video quality"><option value="480">480p</option><option value="720" selected>720p</option><option value="1080">1080p</option></select></div></div>
          </div>
          <div class="sc-pane report">
            <div class="sc-rp">
              <div class="sc-prog"><span class="sc-ic">${I("film")}</span><div class="w"><b class="sc-progt">Clip saved</b><small class="sc-progs">Reviewing…</small>
                <div class="sc-pb"><i class="sc-progbar"></i></div></div></div>
              <div class="sc-rb"></div>
            </div>
          </div>
        </div>
      </div>
      <div class="sc-msg"><div><span class="sc-ic">${I("warn")}</span><div class="sc-msgtext"></div>
        <div class="r"><button class="sc-bt" data-a="close">Close</button><button class="sc-bt p" data-a="retry">Try again</button></div></div></div>`;
    document.body.appendChild(root);
    const video = $(root, ".sc-video"), canvas = $(root, ".sc-canvas"), ctx2 = canvas.getContext("2d");
    const grab = document.createElement("canvas"), gctx = grab.getContext("2d");
    let ws = null, stream = null, track = null, facing = "environment", waitingAck = false, ackTimer = null, lastSent = 0, relayFps = 0;
    let detections = [], closed = false, thinking = false, retry = 0, ping = null, muted = false, torchOn = false, wakeLock = null;
    let recorder = null, recChunks = [], recStart = 0, mic = null, coachOn = false, coachAsked = 0, lastQ = null, listening = true;
    let slow = false, base = "connecting…", conn = "connecting…", goal = "", proc = null, wantReport = false, haveReport = false;
    const t0 = Date.now(), viewers = new Set(), blobs = [];
    const peers = {}, ICE = [{ urls: ["stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478"] }];

    // ------------------------------------------------------------ status line, chips, toasts, banners
    function renderSub() {
      const st = conn || (!listening ? "Mic off" : vad.rec ? "Listening…" : thinking ? "Thinking…" : talkingNow() ? "Speaking…" : goal ? "goal: " + goal : base);
      $(root, ".sc-sub").textContent = `${st} · ${mmss(Date.now() - t0)}`;
      if (recorder) { const t = mmss(Date.now() - recStart); $(root, ".sc-rect").textContent = "REC " + t; $(root, ".sc-reclbl").textContent = t; }
    }
    const clock = setInterval(renderSub, 1000);
    const tag = (cls, on) => $(root, ".sc-tag." + cls).classList.toggle("show", !!on);
    function fatal(text) { $(root, ".sc-msgtext").textContent = text; $(root, ".sc-msg").style.display = "grid"; }
    function toast(text, kind, ms) {
      const t = $(root, ".sc-toast"); t.className = "sc-toast show " + (kind || ""); $(root, ".sc-toastt").textContent = text;
      clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("show"), ms || 3200);
    }
    function banner(status, title, text) {
      const b = $(root, ".sc-banner"); b.className = "sc-banner show " + (status === "danger" ? "danger" : "warning");
      $(root, ".sc-bi").innerHTML = I(status === "danger" ? "stop" : "warn"); $(root, ".sc-bt1").textContent = title; $(root, ".sc-bt2").textContent = text || "";
      if (navigator.vibrate && status === "danger") navigator.vibrate([80, 60, 80]);
      clearTimeout(b._t); b._t = setTimeout(() => b.classList.remove("show"), 9000);
    }
    // old single "badge": warnings/dangers become a banner, everything else a toast
    function badge(status, text) { if (status === "warning" || status === "danger") banner(status, text); else toast(text, status === "ok" ? "ok" : ""); }

    // ------------------------------------------------------------ captions
    function capHTML(text, meta, title) { return esc(text) + (meta ? `<small${title ? ` title="${esc(title)}"` : ""}>${esc(meta)}</small>` : ""); }
    function cap(role, text, meta) {
      const caps = $(root, ".sc-caps"); [...caps.children].forEach((c) => c.classList.add("dim"));
      const d = document.createElement("div"); d.className = "sc-cap" + (role === "me" ? " me" : "");
      d.innerHTML = capHTML(text, meta); caps.appendChild(d);
      while (caps.children.length > 3) caps.removeChild(caps.firstChild);
      return d;
    }
    function thinkingCap() { const d = cap("her", ""); d.innerHTML = '<span class="sc-dots"><span></span><span></span><span></span></span>'; d.dataset.t = "1"; return d; }
    function work(text) { const w = $(root, ".sc-work"); w.classList.toggle("show", !!text); if (text) $(root, ".sc-workt").textContent = `${o.title} is working: ${text}`; }

    // files the brain made (PDFs, images…): tappable chips, images as thumbnails
    function fileChips(files) {
      const box = document.createElement("div"); box.className = "sc-fl";
      for (const f of files) {
        const url = `${o.files}/${encodeURIComponent(f.id)}`, name = f.name || `file ${f.id}`, type = f.type || "";
        const ext = (name.includes(".") ? name.split(".").pop() : type.split("/").pop() || "file").toUpperCase().slice(0, 4);
        const kind = /pdf/i.test(ext) ? "pdf" : /DOC|ODT|RTF/.test(ext) ? "doc" : /XLS|CSV|ODS/.test(ext) ? "xls" : "";
        const img = type.startsWith("image/");
        const a = document.createElement("a"); a.className = "sc-file"; a.href = url; a.target = "_blank"; a.rel = "noopener";
        a.innerHTML = (img ? `<img alt="">` : `<span class="fi ${kind}">${esc(ext)}</span>`) + `<span class="fn"><b>${esc(name)}</b><small>${img ? "Image" : esc(ext)} · tap to open</small></span>`;
        box.appendChild(a);
        fetch(url, { credentials: "same-origin" }).then((r) => r.ok ? r.blob() : null).then((b) => {
          if (!b || closed) return; const h = URL.createObjectURL(b); blobs.push(h); a.href = h;
          if (img) $(a, "img").src = h;
          $(a, "small").textContent = `${img ? "Image" : ext} · ${Math.max(1, Math.round(b.size / 1024))} KB`;
        }).catch(() => {});
      }
      return box;
    }

    // ------------------------------------------------------------ camera
    async function startCamera() {
      if (!window.isSecureContext) { fatal("The camera needs a secure (https) page."); return false; }
      if (!navigator.mediaDevices?.getUserMedia) { fatal("This browser can't use the camera."); return false; }
      if (stream) stream.getVideoTracks().forEach((t) => t.stop());
      const h = +$(root, ".sc-res").value;
      for (const v of [{ facingMode: { ideal: facing }, height: { ideal: h }, width: { ideal: Math.round(h * 16 / 9) } }, { facingMode: { ideal: facing } }, true]) {
        try { stream = await navigator.mediaDevices.getUserMedia({ video: v, audio: false }); break; }
        catch (e) {
          stream = null;
          if (["NotAllowedError", "SecurityError"].includes(e.name)) { fatal("Camera access is blocked. Allow the camera for this site (the lock icon by the address), then try again."); return false; }
          if (e.name === "NotReadableError") { fatal("The camera is busy in another app. Close it and try again."); return false; }
        }
      }
      if (!stream) { fatal("Couldn't open the camera."); return false; }
      video.srcObject = stream; try { await video.play(); } catch (e) {}
      video.style.transform = facing === "user" ? "scaleX(-1)" : "";   // selfie view mirrored, like a video call (boxes follow in draw)
      track = stream.getVideoTracks()[0];
      track.onended = () => { if (!closed) setTimeout(startCamera, 800); };
      const caps = track.getCapabilities ? track.getCapabilities() : {};
      $(root, '[data-a="torch"]').style.display = caps.torch ? "grid" : "none";
      torchOn = false; $(root, '[data-a="torch"]').classList.remove("on");
      for (const pc of Object.values(peers)) for (const s of pc.getSenders()) if (s.track?.kind === "video") s.replaceTrack(track).catch(() => {});
      return true;
    }
    function frameBlob(max, q) {
      const w = video.videoWidth, h = video.videoHeight; if (!w) return Promise.resolve(null);
      const s = Math.min(1, max / Math.max(w, h)); grab.width = Math.round(w * s); grab.height = Math.round(h * s);
      gctx.drawImage(video, 0, 0, grab.width, grab.height);
      return new Promise((r) => grab.toBlob(r, "image/jpeg", q));
    }
    async function loop() {
      if (closed) return;
      const every = relayFps > 0 ? 1000 / relayFps : 1000;
      if (ws?.readyState === 1 && !waitingAck && performance.now() - lastSent >= every && !document.hidden) {
        const b = await frameBlob(960, .72);
        if (b && ws?.readyState === 1) { waitingAck = true; lastSent = performance.now(); ws.send(await b.arrayBuffer());
          clearTimeout(ackTimer); ackTimer = setTimeout(() => waitingAck = false, 12000); }
      }
      draw(); requestAnimationFrame(loop);
    }
    function rr(x, y, w, h, r) { ctx2.beginPath(); if (ctx2.roundRect) ctx2.roundRect(x, y, w, h, r); else ctx2.rect(x, y, w, h); }
    function draw() {
      const r = canvas.getBoundingClientRect(), W = Math.round(r.width * devicePixelRatio), H = Math.round(r.height * devicePixelRatio);
      if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
      ctx2.clearRect(0, 0, W, H);
      const vw = video.videoWidth, vh = video.videoHeight; if (!vw || !detections.length) return;
      const dp = devicePixelRatio, sc = Math.max(W / vw, H / vh), ox = (W - vw * sc) / 2, oy = (H - vh * sc) / 2, fs = grab.width ? vw / grab.width : 1;
      ctx2.lineWidth = 1.5 * dp; ctx2.font = `600 ${11 * dp}px system-ui`;
      const mirror = facing === "user";
      for (const d of detections) { if (!d.box) continue; const [x1, y1, x2, y2] = d.box.map((v) => v * fs * sc);
        const w = x2 - x1, x = mirror ? W - (ox + x1) - w : ox + x1, y = oy + y1;
        ctx2.strokeStyle = "rgba(255,255,255,.75)"; rr(x, y, w, y2 - y1, 8 * dp); ctx2.stroke();
        const l = `${d.label} ${Math.round(d.conf * 100)}%`, lw = ctx2.measureText(l).width + 12 * dp, lh = 18 * dp;
        const lx = Math.min(Math.max(4 * dp, x), W - lw - 4 * dp), ly = y - lh - 4 * dp < 4 * dp ? y + 4 * dp : y - lh - 4 * dp;
        ctx2.fillStyle = "rgba(0,0,0,.55)"; rr(lx, ly, lw, lh, 9 * dp); ctx2.fill();
        ctx2.fillStyle = "#fff"; ctx2.fillText(l, lx + 6 * dp, ly + 13 * dp); }
    }

    // ------------------------------------------------------------ voice: always listening (hands-free), barge-in
    let speakUntil = 0;
    function say(t) {
      if (muted || !t || !window.speechSynthesis) return;
      try { speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(t); u.rate = 1.05; speakUntil = performance.now() + 60000;
        u.onend = u.onerror = () => speakUntil = performance.now() + 500; speechSynthesis.speak(u); } catch (e) {}
    }
    const talkingNow = () => performance.now() < speakUntil || window.speechSynthesis?.speaking;
    const vad = { an: null, buf: null, floor: .01, sp: 0, si: 0, rec: null, chunks: [], t0: 0, timer: null, ac: null };
    async function startListening() {
      try { mic = mic || await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }); }
      catch (e) { listening = false; setTalk(); toast("Microphone blocked: allow it to talk, or type under More.", "", 6000); return; }
      const AC = window.AudioContext || window.webkitAudioContext; const ac = new AC(); vad.ac = ac; await ac.resume().catch(() => {});
      const src = ac.createMediaStreamSource(mic); vad.an = ac.createAnalyser(); vad.an.fftSize = 1024; src.connect(vad.an);
      vad.buf = new Float32Array(1024); clearInterval(vad.timer); vad.timer = setInterval(tick, 50);
    }
    const micBtn = () => $(root, '[data-a="talk"]');
    function tick() {
      if (!listening || !vad.an) return;
      vad.an.getFloatTimeDomainData(vad.buf); let s = 0; for (const v of vad.buf) s += v * v; const rms = Math.sqrt(s / vad.buf.length);
      const over = talkingNow(), th = Math.max(.015, vad.floor * (over ? 6 : 3));
      micBtn().style.setProperty("--lvl", Math.min(1, Math.max(0, (rms - vad.floor) / (th * 2.5))).toFixed(2));
      if (!vad.rec && rms < th) vad.floor = vad.floor * .98 + rms * .02;
      const voiced = rms > th;
      if (!vad.rec) { vad.sp = voiced ? vad.sp + 50 : 0; if (vad.sp >= 150) { if (over) { try { speechSynthesis.cancel(); } catch (e) {} speakUntil = 0; } segStart(); } return; }
      vad.si = voiced ? 0 : vad.si + 50;
      if (vad.si >= 850 || performance.now() - vad.t0 > 15000) vad.rec.stop();
    }
    function segStart() {
      if (!window.MediaRecorder) return;
      const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
      try { vad.rec = new MediaRecorder(mic, type ? { mimeType: type } : undefined); } catch (e) { vad.rec = null; return; }
      vad.chunks = []; vad.t0 = performance.now(); vad.si = 0; micBtn().classList.add("listen"); renderSub();
      vad.rec.ondataavailable = (e) => e.data.size && vad.chunks.push(e.data);
      vad.rec.onstop = async () => {
        const mime = vad.rec.mimeType || "audio/webm", dur = performance.now() - vad.t0; vad.rec = null; vad.sp = 0;
        micBtn().classList.remove("listen"); renderSub();
        if (dur < 600) return;
        const f = new FormData(); f.append("file", new Blob(vad.chunks, { type: mime }), "q." + (mime.includes("mp4") ? "mp4" : mime.includes("ogg") ? "ogg" : "webm"));
        try { const r = await fetch(o.stt, { method: "POST", body: f, credentials: "same-origin" }); const j = await r.json(); const t = (j.text || "").trim();
          if (r.ok && t.length > 1 && !/^(thank you\.?|thanks for watching\.?|you\.?|\.+)$/i.test(t)) ask(t, true);
          else if (!r.ok) toast(`Couldn't hear that (voice error ${r.status}). Type under More instead.`, "", 5000);
        } catch (e) { toast("Couldn't send your voice: no connection"); }
      };
      vad.rec.start(250);
    }
    function setTalk() {
      const b = micBtn(); b.classList.toggle("off", !listening); b.style.setProperty("--lvl", 0);
      $(root, ".sc-micic").innerHTML = I(listening ? "mic" : "micoff"); $(root, ".sc-talklbl").textContent = listening ? "Mute" : "Unmute";
      b.setAttribute("aria-label", listening ? "Mute microphone" : "Unmute microphone"); renderSub();
    }

    // ------------------------------------------------------------ the link
    function send(o2) { if (ws?.readyState === 1) { ws.send(JSON.stringify(o2)); return true; } toast("Reconnecting…"); return false; }
    function ask(text, byVoice) {
      if (!text) return;
      if (thinking) { if (byVoice) say("One moment."); return; }
      if (!send({ type: "ask", text })) return;
      cap("me", text); lastQ = thinkingCap(); if (byVoice || slow) say(slow ? "Let me look. This takes a minute or two." : "Let me look.");
    }
    function connect() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const url = o.ws.startsWith("ws") ? o.ws : `${proto}://${location.host}${o.ws}`;
      ws = new WebSocket(url); ws.binaryType = "arraybuffer";
      ws.onopen = () => { retry = 0; waitingAck = false; clearInterval(ping); ping = setInterval(() => send({ type: "ping", t: Date.now() }), 20000); };
      ws.onclose = (e) => {
        clearInterval(ping); if (closed) return;
        if (e.code === 4401) { fatal("Your login expired. Close this and log in again."); return; }
        if (e.code === 4404) return;
        conn = "reconnecting…"; renderSub(); setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++));
      };
      ws.onmessage = (e) => { if (typeof e.data !== "string") return; let m; try { m = JSON.parse(e.data); } catch (x) { return; } handle(m); };
    }
    const reading = (rd, colour) => { const c = $(root, ".sc-reading"); c.textContent = `${(+rd.value).toFixed(1)} ${rd.unit || ""}`; c.classList.add("show");
      if (colour) c.classList.toggle("out", rd.in_range === false); };
    function handle(m) {
      switch (m.type) {
        case "hello":
          slow = m.ready && !/^fast/.test(m.note || ""); conn = "";
          base = !m.ready ? `model not ready: ${m.note || ""}` : slow ? "slow mode · answers take 1–2 min" : "ready · just talk"; renderSub();
          if (slow) toast("Slow mode: no GROQ_API_KEY or GEMINI_API_KEY on the vision server, so answers take 1–2 minutes", "", 8000);
          fillProcs(m.procedures || []); break;
        case "frame_ack": waitingAck = false; clearTimeout(ackTimer); break;
        case "perception": detections = [...(m.detections || [])]; if (m.reading) reading(m.reading, true); break;
        case "thinking": thinking = true; if (!lastQ) lastQ = thinkingCap(); renderSub(); break;
        case "busy": thinking = false; renderSub(); if (lastQ) { lastQ.innerHTML = capHTML("Still on the last one, ask again in a second."); delete lastQ.dataset.t; lastQ = null; } break;
        case "answer": {
          thinking = false; renderSub();
          if (m.coach && m.quiet) { if (lastQ?.dataset.t) { lastQ.remove(); lastQ = null; } break; }
          const meta = [m.coach ? "coach" : "", m.latency_ms ? (m.latency_ms / 1000).toFixed(1) + " s" : "", m.sources?.length ? m.sources.join(", ") : ""].filter(Boolean).join(" · ");
          if (lastQ && lastQ.dataset.t) { lastQ.innerHTML = capHTML(m.say, meta, m.model); delete lastQ.dataset.t; } else { const d = cap("her", m.say, meta); if (m.model) d.innerHTML = capHTML(m.say, meta, m.model); }
          lastQ = null;
          if (m.status === "warning" || m.status === "danger") banner(m.status, m.status === "danger" ? "Stop" : "Check this", m.say);
          else if (m.status && m.status !== "ok") toast({ unsure: "Not sure about this one" }[m.status] || m.status);
          if (m.reading) reading(m.reading, false);
          say(m.say + (m.note ? " " + m.note : ""));
          break; }
        case "alert": banner(m.status || "warning", m.status === "danger" ? "Stop" : "Heads up", m.say); cap("her", m.say, "alert"); say(m.say); break;
        case "state": showState(m); break;
        case "coach": {
          const was = coachOn; coachOn = m.seconds > 0; $(root, '[data-a="coach"]').classList.toggle("on", coachOn); tag("coach", coachOn);
          if (coachOn && !was) toast("Coach is on — I'll speak up only if something's wrong", "coach", 4500);
          else if (!coachOn && was) toast("Coach is off"); break; }
        case "relay": relayFps = m.fps || 0; break;
        case "viewer_join": viewers.add(m.viewer); showViewers(); offer(m.viewer); break;
        case "viewer_leave": viewers.delete(m.viewer); showViewers(); if (peers[m.viewer]) { peers[m.viewer].close(); delete peers[m.viewer]; } break;
        case "rtc": onRtc(m.viewer, m.data); break;
        case "job": watchJob(m.id); break;
        case "video_report": report(m); break;
        case "error":
          if (m.fatal) { fatal(m.text); break; }
          if (/goal/i.test(m.text || "") && performance.now() - coachAsked < 20000) { toast(m.text, "", 5000); openGoal(); break; }
          toast(m.text, "", 5000); break;
        // from Iriis's own brain (tools she runs while you're on camera)
        case "brain_thinking": work(m.text || "on it…"); break;
        case "brain_done": work(""); break;
        case "brain": {
          work("");
          const d = cap("her", m.say || (m.files?.length ? "Here you go." : ""));
          if (m.files?.length) d.appendChild(fileChips(m.files));
          say(m.say); break; }
        case "brain_confirm": confirmCard(m); break;
      }
    }
    function showViewers() { tag("view", viewers.size > 0); $(root, ".sc-viewn").textContent = viewers.size; }
    function showState(s) {
      const box = $(root, ".sc-step");
      if (s.goal !== undefined) goal = s.goal || "";
      $(root, ".sc-d-goal").textContent = goal ? "Now: " + goal : "Tell it what you're doing, so Coach can help";
      if (!s.procedure) { proc = null; box.classList.remove("show"); $(root, ".sc-run").classList.remove("show");
        $(root, ".sc-d-proc").textContent = "Guide me through a saved procedure"; renderSub(); return; }
      proc = s; box.classList.add("show");
      const n = s.done ? s.steps : (s.step_number || 1), pct = s.steps ? Math.round(100 * (s.done ? s.steps : n - 1) / s.steps) : 0;
      const head = s.done ? `${s.procedure} · done` : `Step ${n} of ${s.steps}`, text = s.done ? `All ${s.steps} steps done` : s.step.instruction;
      $(root, ".sc-stepk").textContent = head; $(root, ".sc-steptext").textContent = text;
      $(root, ".sc-stepbar").style.width = $(root, ".sc-runbar").style.width = (s.done ? 100 : pct) + "%";
      $(root, ".sc-run").classList.add("show"); $(root, ".sc-runn").textContent = `${s.procedure} · ${head}`; $(root, ".sc-runs").textContent = text;
      $(root, ".sc-d-proc").textContent = `Running: ${s.procedure} · ${s.done ? "done" : `step ${n} of ${s.steps}`}`;
      if (!s.done) say(`Step ${s.step_number}. ${s.step.instruction}`);
    }
    async function fillProcs(list) { const sel = $(root, ".sc-procs"); sel.length = 1;
      for (const p of list) { const op = document.createElement("option"); op.value = p.file; op.textContent = `${p.name} (${p.steps} steps)`; sel.appendChild(op); } }

    // the brain asks before doing something on your behalf
    function confirmCard(m) {
      const box = $(root, ".sc-cfs"), c = document.createElement("div"); c.className = "sc-cf";
      c.innerHTML = `<div class="lb">${I("spark")}${esc(o.title)} asks</div><div class="q">${esc(m.question)}</div>
        <div class="bt"><button class="y" data-a="cf:yes">Yes</button><button data-a="cf:no">No</button><button class="all" data-a="cf:yes to all">Yes to all (10 min)</button></div>`;
      c._m = m; box.appendChild(c);
      if (navigator.vibrate) navigator.vibrate(40);
      say(m.question);
    }
    async function answerConfirm(c, text) {
      const m = c._m; c.classList.add("busy");
      try {
        const r = await fetch(o.confirm, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: m.session_id, id: m.id, text }) });
        if (!r.ok) throw new Error(r.status);
        c.remove(); toast("Sent", "ok", 1800);
      } catch (e) { c.classList.remove("busy"); toast("Couldn't send your answer"); }
    }

    // live video to watch screens (WebRTC), as on the Seryn Vision page
    async function offer(viewer) {
      if (!stream || !window.RTCPeerConnection) return; if (peers[viewer]) peers[viewer].close();
      const pc = new RTCPeerConnection({ iceServers: ICE }); peers[viewer] = pc;
      stream.getVideoTracks().forEach((t) => pc.addTrack(t, stream));
      pc.onicecandidate = (e) => e.candidate && send({ type: "rtc", viewer, data: { candidate: e.candidate } });
      try { await pc.setLocalDescription(await pc.createOffer()); send({ type: "rtc", viewer, data: { sdp: pc.localDescription } }); } catch (e) {}
    }
    async function onRtc(v, d) { const pc = peers[v]; if (!pc || !d) return;
      try { if (d.sdp) await pc.setRemoteDescription(d.sdp); else if (d.candidate) await pc.addIceCandidate(d.candidate); } catch (e) {} }

    // ------------------------------------------------------------ reports (recorded clips, "what just happened")
    function progress(title, sub, pct, state) {
      const p = $(root, ".sc-prog"); p.className = "sc-prog" + (state ? " " + state : "");
      $(root, ".sc-progt").textContent = title; $(root, ".sc-progs").textContent = sub;
      $(p, ".sc-pb").classList.toggle("ind", pct == null);
      $(root, ".sc-progbar").style.width = pct == null ? "" : Math.round(pct * 100) + "%";
    }
    let lastKind = "video";
    function reportSheet(kind) {
      lastKind = kind || lastKind; sheet(true, "report");
      $(root, ".sc-shtitle").textContent = lastKind === "recent" ? "What just happened" : "Clip review";
    }
    async function watchJob(id) {
      const recent = !recorder && wantReport === "recent";
      if (wantReport || $(root, ".sc-sheet").classList.contains("open")) reportSheet(recent ? "recent" : "video");
      $(root, ".sc-rb").innerHTML = "";
      progress(recent ? "Looking back" : "Clip saved", "Reviewing…", 0);
      for (let i = 0; i < 600 && !closed; i++) {
        try { const r = await fetch(`${o.api}/jobs/${id}`, { credentials: "same-origin" }); if (!r.ok) return; const j = await r.json();
          if (j.status !== "running") return report(j);
          progress(recent ? "Looking back" : "Clip saved", `Reviewing… ${Math.round((j.progress || 0) * 100)}%`, j.progress || 0); } catch (e) {}
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    function evStatus(ev, problems) {
      const t = String(ev || "").toLowerCase();
      if (/\b(danger|stop|fire|smoke|burn|unsafe)\b/.test(t)) return "danger";
      if (problems.some((p) => p && t.includes(String(p).toLowerCase().slice(0, 18))) || /\b(warn|too (hot|cold|high|low)|out of range|problem|error|wrong|spill|missed)\b/.test(t)) return "warning";
      return "";
    }
    function report(j) {
      const r = j.result || {}, kind = j.kind === "recent" ? "recent" : "video", rb = $(root, ".sc-rb");
      haveReport = true; $(root, '.sc-item[data-k="lastreport"]').classList.remove("hide");
      if (j.status === "failed") {
        progress("Review failed", j.error || "Something went wrong", 1, "fail done"); rb.innerHTML = "";
      } else {
        progress(kind === "recent" ? "Last 90 seconds" : "Clip reviewed", [r.duration ? "length " + r.duration : "", r.frames ? r.frames + " frames" : ""].filter(Boolean).join(" · ") || "Done", 1, "done");
        const probs = r.problems || [];
        rb.innerHTML = (r.summary ? `<p class="sum">${esc(r.summary)}</p>` : "") +
          (r.answer ? `<div class="ans">${esc(r.answer)}</div>` : "") +
          probs.map((p) => `<div class="prob">${I("warn")}<span>${esc(p)}</span></div>`).join("") +
          ((r.timeline || []).length ? `<h5>Timeline</h5><ol class="sc-tl">${r.timeline.map((e) => `<li class="${e.status || evStatus(e.event, probs)}"><time>${esc(e.t)}</time><span>${esc(e.event)}</span></li>`).join("")}</ol>` : "");
      }
      lastKind = kind; if (wantReport) reportSheet(kind); wantReport = false;
      const d = cap("her", r.summary || (j.status === "failed" ? "The review failed." : "Report ready."), kind === "recent" ? "what just happened" : "video report");
      d.insertAdjacentHTML("beforeend", `<button class="lnk" data-a="lastreport">${I("film")}View report</button>`);
      say(r.summary || "");
    }

    // ------------------------------------------------------------ sheet
    function sheet(show, pane) {
      const s = $(root, ".sc-sheet"), on = show ?? !s.classList.contains("open");
      if (pane) s.dataset.pane = pane;
      if (on && pane !== "report" && s.dataset.pane === "more") $(root, ".sc-shtitle").textContent = "More";
      s.classList.toggle("open", on); $(root, ".sc-dim").classList.toggle("open", on); s.style.transform = "";
      if (!on) { document.activeElement?.blur?.(); }
    }
    function expand(k, only) {
      root.querySelectorAll(".sc-item[data-k]").forEach((it) => { if (it.dataset.k === k) it.classList.toggle("open", only ? true : undefined); else it.classList.remove("open"); });
    }
    function openGoal() { sheet(true, "more"); expand("goal", true); setTimeout(() => $(root, ".sc-goal").focus(), 260); }
    (function dragSheet() {   // drag the handle down to close
      const s = $(root, ".sc-sheet"), h = $(root, ".sc-shh"); let y0 = null, dy = 0;
      h.addEventListener("pointerdown", (e) => { if (e.target.closest("button")) return; y0 = e.clientY; dy = 0; s.classList.add("drag"); h.setPointerCapture(e.pointerId); });
      h.addEventListener("pointermove", (e) => { if (y0 == null) return; dy = Math.max(0, e.clientY - y0); s.style.transform = `translate(-50%,${dy}px)`; });
      const end = () => { if (y0 == null) return; y0 = null; s.classList.remove("drag"); if (dy > 80) sheet(false); else s.style.transform = ""; };
      h.addEventListener("pointerup", end); h.addEventListener("pointercancel", end);
    })();

    // ------------------------------------------------------------ buttons
    async function upload(blob, kind, name) {
      const f = new FormData(); f.append("file", blob, name); f.append("kind", kind);
      const r = await fetch(`${o.api}/upload`, { method: "POST", body: f, credentials: "same-origin" }); return r.ok ? r.json() : null;
    }
    async function act(a) {
      if (a.startsWith("x:")) { expand(a.slice(2)); const it = $(root, `.sc-item[data-k="${a.slice(2)}"]`);
        if (it.classList.contains("open")) { const inp = it.querySelector("input.sc-in"); if (inp && matchMedia("(pointer:fine)").matches) setTimeout(() => inp.focus(), 230); } return; }
      if (a.startsWith("cf:")) return;
      switch (a) {
        case "close": close(); break;
        case "retry": $(root, ".sc-msg").style.display = "none"; if (await startCamera()) { if (!ws || ws.readyState > 1) connect(); } break;
        case "flip": facing = facing === "environment" ? "user" : "environment"; startCamera(); break;
        case "torch": torchOn = !torchOn; try { await track.applyConstraints({ advanced: [{ torch: torchOn }] }); } catch (e) { torchOn = false; }
          $(root, '[data-a="torch"]').classList.toggle("on", torchOn); break;
        case "talk": listening = !listening; setTalk(); if (listening && !vad.an) startListening(); break;
        case "coach":
          if (!coachOn && !proc && !goal) { toast("Tell me the goal first, then Coach can watch for mistakes", "", 4500); openGoal(); break; }
          coachAsked = performance.now(); send({ type: "coach", seconds: coachOn ? 0 : 25 }); break;
        case "recent": if (send({ type: "recent", seconds: 90 })) { cap("me", "What did I just do?"); wantReport = "recent";
          $(root, ".sc-rb").innerHTML = ""; progress("Looking back", "Reviewing the last 90 seconds…", null); reportSheet("recent"); } break;
        case "lastreport": if (haveReport || $(root, ".sc-rb").innerHTML) reportSheet(); break;
        case "more": sheet(true, "more"); break;
        case "sheetclose": sheet(false); break;
        case "banner": $(root, ".sc-banner").classList.remove("show"); break;
        case "ask": { const q = $(root, ".sc-q"); ask(q.value.trim()); q.value = ""; sheet(false); break; }
        case "goal": { const g = $(root, ".sc-goal"); if (g.value.trim()) { goal = g.value.trim(); send({ type: "goal", text: goal }); g.value = "";
          $(root, ".sc-d-goal").textContent = "Now: " + goal; renderSub(); sheet(false); toast("Goal set: " + goal, "ok"); } break; }
        case "start": { const v = $(root, ".sc-procs").value; if (v) { send({ type: "procedure", name: v }); sheet(false); } break; }
        case "prev": send({ type: "step", action: "prev" }); break;
        case "next": send({ type: "step", action: "next" }); break;
        case "check": send({ type: "step", action: "check" }); lastQ = thinkingCap(); sheet(false); break;
        case "import": { const p = $(root, ".sc-seryn").value.trim(); if (!p) break;
          const r = await fetch(`${o.api}/procedures/import-seryn`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ protocol: p }) });
          const j = await r.json().catch(() => ({})); toast(r.ok ? `Imported ${j.name || p}` : (j.detail || "Import failed"), r.ok ? "ok" : "");
          if (r.ok) { $(root, ".sc-seryn").value = ""; const pr = await fetch(`${o.api}/procedures`, { credentials: "same-origin" }); if (pr.ok) fillProcs(await pr.json()); } break; }
        case "upload": { const fs = $(root, ".sc-files").files; if (!fs.length) { $(root, ".sc-files").click(); break; } const f = new FormData(); for (const x of fs) f.append("files", x, x.name);
          toast("Uploading…");
          const r = await fetch(`${o.api}/knowledge/upload`, { method: "POST", body: f, credentials: "same-origin" }).catch(() => null);
          toast(r?.ok ? "Added to what it knows" : "Upload failed", r?.ok ? "ok" : "");
          if (r?.ok) { $(root, ".sc-files").value = ""; $(root, ".sc-filesl").textContent = "Choose files…"; } break; }
        case "addurl": { const u = $(root, ".sc-url").value.trim(); if (!/^https?:\/\//.test(u)) { toast("Paste a link that starts with https://"); break; }
          const r = await fetch(`${o.api}/knowledge/url`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: u }) }).catch(() => null);
          toast(r?.ok ? "Link added" : "Couldn't add that link", r?.ok ? "ok" : ""); if (r?.ok) $(root, ".sc-url").value = ""; break; }
        case "snap": { sheet(false); const b = await frameBlob(4096, .92); if (b && await upload(b, "photo", "snap.jpg").catch(() => null)) toast("Photo saved", "ok"); else toast("Couldn't save the photo"); break; }
        case "mute": muted = !muted; $(root, ".sc-mutesw").classList.toggle("on", !muted); if (muted) try { speechSynthesis.cancel(); } catch (e) {} break;
        case "record": await toggleRecord(); break;
      }
    }
    async function toggleRecord() {
      const btn = $(root, '[data-a="record"]');
      if (recorder) { recorder.stop(); return; }
      if (!window.MediaRecorder || !stream) { toast("Recording isn't supported here"); return; }
      let rs = stream; try { if (mic) rs = new MediaStream([...stream.getVideoTracks(), ...mic.getAudioTracks()]); } catch (e) {}
      const type = ["video/webm;codecs=vp9,opus", "video/webm", "video/mp4"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
      try { recorder = new MediaRecorder(rs, type ? { mimeType: type } : undefined); } catch (e) { toast("Recording isn't supported here"); return; }
      recChunks = []; recorder.ondataavailable = (e) => e.data.size && recChunks.push(e.data);
      recorder.onstop = async () => {
        const blob = new Blob(recChunks, { type: recorder.mimeType }), len = mmss(Date.now() - recStart); recorder = null;
        btn.classList.remove("rec"); tag("rec", false); $(root, ".sc-reclbl").textContent = "Record"; btn.setAttribute("aria-label", "Record a clip");
        wantReport = "video"; $(root, ".sc-rb").innerHTML = ""; progress("Saving clip", `${len} · uploading…`, null); reportSheet("video");
        const j = await upload(blob, "video", blob.type.includes("mp4") ? "clip.mp4" : "clip.webm").catch(() => null);
        if (!j) { progress("Couldn't save the clip", "Check your connection and try again", 1, "fail done"); return; }
        progress("Clip saved", "Reviewing…", 0);
        const r = await fetch(`${o.api}/media/${j.id}/analyze`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => null);
        if (r?.ok) watchJob((await r.json()).job); else progress("Clip saved", "Couldn't start the review", 1, "fail done");
      };
      recorder.start(1000); recStart = Date.now(); btn.classList.add("rec"); tag("rec", true); btn.setAttribute("aria-label", "Stop recording"); renderSub();
    }
    root.addEventListener("click", (e) => {
      const b = e.target.closest("[data-a]"); if (!b) return;
      if (b.dataset.a.startsWith("cf:")) { answerConfirm(b.closest(".sc-cf"), b.dataset.a.slice(3)); return; }
      act(b.dataset.a);
    });
    const enter = (sel, a) => $(root, sel).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); act(a); } });
    enter(".sc-q", "ask"); enter(".sc-goal", "goal"); enter(".sc-url", "addurl"); enter(".sc-seryn", "import");
    $(root, ".sc-files").addEventListener("change", (e) => { const n = e.target.files.length;
      $(root, ".sc-filesl").textContent = !n ? "Choose files…" : n === 1 ? e.target.files[0].name : `${n} files`; });
    $(root, ".sc-res").addEventListener("change", startCamera);
    $(root, '.sc-item[data-k="lastreport"]').classList.add("hide");
    const vis = () => { if (!document.hidden && track?.readyState === "ended") startCamera(); };
    document.addEventListener("visibilitychange", vis);
    const onKey = (e) => { if (e.key === "Escape" && $(root, ".sc-sheet").classList.contains("open")) sheet(false); };
    document.addEventListener("keydown", onKey);

    function close() {
      if (closed) return;
      closed = true; clearInterval(ping); clearInterval(vad.timer); clearInterval(clock);
      try { if (recorder && recorder.state !== "inactive") { recorder.onstop = null; recorder.stop(); } } catch (e) {}
      try { ws?.close(); } catch (e) {} try { speechSynthesis.cancel(); } catch (e) {}
      stream?.getTracks().forEach((t) => t.stop()); mic?.getTracks().forEach((t) => t.stop()); try { vad.ac?.close(); } catch (e) {}
      Object.values(peers).forEach((p) => p.close()); try { wakeLock?.release(); } catch (e) {}
      blobs.forEach((u) => URL.revokeObjectURL(u));
      document.removeEventListener("visibilitychange", vis); document.removeEventListener("keydown", onKey); root.remove(); o.onClose && o.onClose();
    }

    renderSub();
    (async () => {
      try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch (e) {}
      try { const u = new SpeechSynthesisUtterance(" "); u.volume = 0; speechSynthesis.speak(u); } catch (e) {}   // iOS: unlock voice
      if (await startCamera()) { connect(); requestAnimationFrame(loop); startListening(); }
    })();
    return { close, ask };
  }
  window.SerynCamera = { open };
})();
