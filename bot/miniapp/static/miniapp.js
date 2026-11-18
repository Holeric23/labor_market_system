const telegram = window.Telegram && window.Telegram.WebApp;
const status = document.getElementById('miniapp-status');
if (telegram && telegram.initData) {
  telegram.ready();
  telegram.expand();
  document.getElementById('init-data').value = telegram.initData;
  document.getElementById('miniapp-login').submit();
} else {
  status.textContent = 'Откройте администрирование кнопкой в личном чате с ботом.';
}
