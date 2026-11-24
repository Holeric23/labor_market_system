const telegram = window.Telegram && window.Telegram.WebApp;
const status = document.getElementById('miniapp-status');
if (telegram && telegram.initData) {
  status.textContent = 'Проверяем доступ через Telegram…';
  telegram.ready();
  telegram.expand();
  document.getElementById('init-data').value = telegram.initData;
  document.getElementById('miniapp-login').submit();
} else {
  status.textContent = 'Для доступа откройте панель в Telegram с аккаунта владельца.';
}
