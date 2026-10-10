import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import zh from './zh-CN.json'
import en from './en-US.json'

export const LOCALE_KEY = 'tl_locale'
i18n.use(initReactI18next).init({
  resources: { 'zh-CN': { translation: zh }, 'en-US': { translation: en } },
  lng: localStorage.getItem(LOCALE_KEY) || 'zh-CN',
  fallbackLng: 'zh-CN',
  interpolation: { escapeValue: false },
})
export default i18n
