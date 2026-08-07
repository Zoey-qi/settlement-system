/* ============================================================
   登录页背景：Speed Line（集中线）效果
   基于 wangyasai.github.io/Speed-Line 的 p5.js 算法改写：
   - 本地自托管 p5（无第三方 CDN）
   - 暗金金属色系，呼应图标着色
   - 透明背景叠加在登录区渐变/光球之上，聚焦到登录卡片
   - 静态绘制（noLoop），resize 时重绘，性能友好
   ============================================================ */
(function () {
  'use strict';

  // 暗金金属色系（与图标 --c-icon:#B8860B 及 gold-metal 渐变呼应）
  const GOLD = ['#B8860B', '#C9A227', '#D9B44A', '#9A6E08', '#E6C66E'];

  const sketch = (p) => {
    let r = 0;
    const opts = {
      Counts: 150,    // 线条数量
      Width: 20,      // 线条根部宽度
      Length: 560,    // 线条长度
      CenterX: 0,
      CenterY: 0
    };
    let host;

    function dims() {
      host = document.querySelector('.auth-main') || document.body;
      return {
        w: host.clientWidth || window.innerWidth,
        h: host.clientHeight || window.innerHeight
      };
    }

    p.setup = function () {
      const { w, h } = dims();
      const c = p.createCanvas(w, h);
      const mount = document.getElementById('speedline-canvas');
      if (mount) c.parent(mount);
      p.noLoop();
      r = Math.max(w, h) * 1.12;
      drawLines();
    };

    function drawLines() {
      p.clear();
      const cx = p.width / 2 + opts.CenterX;
      const cy = p.height / 2 + opts.CenterY;
      p.noStroke();
      for (let i = 0; i < p.TWO_PI; i += p.TWO_PI / opts.Counts) {
        p.push();
        p.translate(cx, cy);
        p.rotate(i);
        p.fill(p.random(GOLD));
        p.beginShape();
        p.vertex(-p.random(opts.Width), r);
        p.vertex(0, r / 2 - p.random(opts.Length));
        p.vertex(p.random(opts.Width), r);
        p.endShape(p.CLOSE);
        p.pop();
      }
    }

    p.windowResized = function () {
      const { w, h } = dims();
      p.resizeCanvas(w, h);
      r = Math.max(w, h) * 1.12;
      drawLines();
    };
  };

  function boot() {
    if (!window.p5) return;
    if (!document.querySelector('.auth-main')) return; // 仅登录页
    new p5(sketch);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
