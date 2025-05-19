// firebase-messaging-sw.js

importScripts('https://www.gstatic.com/firebasejs/10.11.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.11.0/firebase-messaging-compat.js');

firebase.initializeApp({
  apiKey: "AIzaSyAkXj09Qh9vvb0_JVjr_b_jF3QD3eGV5Dw",
  authDomain: "ai-planner-d9808.firebaseapp.com",
  projectId: "ai-planner-d9808",
  storageBucket: "ai-planner-d9808.firebasestorage.app",
  messagingSenderId: "901247348661",
  appId: "1:901247348661:web:2c084fe5bf35638f9073fc"
});

const messaging = firebase.messaging();

// Handle background push notification
messaging.onBackgroundMessage(function(payload) {
  console.log('[firebase-messaging-sw.js] Received background message ', payload);

  const notificationTitle = payload.notification.title || 'AI Planner';
  const notificationOptions = {
    body: payload.notification.body,
    icon: '/icon-192.png'
  };

  self.registration.showNotification(notificationTitle, notificationOptions);
});
