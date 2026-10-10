import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import enUS from 'antd/locale/en_US'
import 'dayjs/locale/zh-cn'
import i18n from './i18n'
import { AuthProvider } from './auth'
import App from './App'

function Root() {
  const [lng, setLng] = React.useState(i18n.language)
  React.useEffect(() => { const h = (l: string) => setLng(l); i18n.on('languageChanged', h); return () => i18n.off('languageChanged', h) }, [])
  return (
    <ConfigProvider locale={lng === 'en-US' ? enUS : zhCN} theme={{ token: { colorPrimary: '#1458e0' } }}>
      <BrowserRouter><AuthProvider><App /></AuthProvider></BrowserRouter>
    </ConfigProvider>
  )
}
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><Root /></React.StrictMode>)
