// Shared chrome for pages that are not the landing app: mobile menu toggle
// and the nav's scrolled state. No data fetching, no dependencies.
(() => {
  'use strict';
  document.documentElement.classList.add('js');

  const burger = document.getElementById('navBurger');
  const mobileNav = document.getElementById('mobileNav');
  if (burger && mobileNav) {
    burger.addEventListener('click', () => {
      const open = mobileNav.classList.toggle('open');
      burger.setAttribute('aria-expanded', String(open));
    });
    mobileNav.addEventListener('click', e => {
      if (e.target.tagName === 'A') {
        mobileNav.classList.remove('open');
        burger.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && mobileNav.classList.contains('open')) {
        mobileNav.classList.remove('open');
        burger.setAttribute('aria-expanded', 'false');
        burger.focus();
      }
    });
  }

  const siteNav = document.querySelector('.sitenav');
  if (siteNav) {
    const onScroll = () => siteNav.classList.toggle('scrolled', window.scrollY > 8);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
  }
})();
