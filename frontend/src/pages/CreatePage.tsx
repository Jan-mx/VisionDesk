import { useNavigate } from 'react-router-dom'

import AssetLibrary from '@/components/AssetLibrary'
import GenerateForm from '@/components/GenerateForm'
import ImageDropzone from '@/components/ImageDropzone'
import { errorMessage } from '@/hooks/useAuth'
import { useAssetLibrary, useUploadAsset } from '@/hooks/useAssets'
import { useGenerate } from '@/hooks/useRun'
import { useCreateSession } from '@/hooks/useSessions'
import { readPromptDraft, savePromptDraft } from '@/lib/promptDraft'

/** 创作控制台（复现 creative-v1 方案 A · 中央创作台） */
export default function CreatePage() {
  const navigate = useNavigate()
  const { data: groups = [], isPending } = useAssetLibrary()
  const upload = useUploadAsset()
  const generate = useGenerate()
  const createSession = useCreateSession()

  const openEditor = (assetId: string) =>
    createSession.mutate(
      { current_asset_id: assetId },
      { onSuccess: (session) => navigate(`/editor/${session.id}`) },
    )

  return (
    <div className="mx-auto max-w-[1100px] px-6 py-8 sm:px-10">
      {/* 页面主标题区 */}
      <div className="mb-6 flex items-center justify-between gap-4">
        <div>
          <div className="eyebrow mb-1 text-muted-light">MAKE SOMETHING WONDERFUL</div>
          <h1 className="text-ink text-[26px] font-bold tracking-tight">今天，让什么变得更好看？</h1>
          <p className="text-muted mt-1 text-xs">从一个想法开始，也可以接着上次的灵感继续。</p>
        </div>
        <div className="hidden items-center gap-2 sm:flex" aria-hidden>
          <i className="bg-peach border-peach-border size-5 rotate-12 rounded-md border block" />
          <i className="bg-mint border-mint-border size-4.5 translate-y-1.5 rounded-full border block" />
          <i className="bg-lavender border-lavender-border size-4.5 -translate-y-1 rotate-[35deg] rounded-md border block" />
        </div>
      </div>

      {/* 中央轻渐变创作台 */}
      <section className="bg-gradient-to-br from-[#f5f2ff] via-[#f6f8ff] to-[#f3faf5] border-[#ebe9f5] rounded-panel shadow-xs border p-6 text-center sm:p-8">
        <div className="border-line bg-paper text-ink-secondary shadow-xs mb-3 inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium">
          <span className="bg-lavender text-brand grid size-4.5 place-items-center rounded-md text-[10px]">
            ✦
          </span>
          <span>你的 AI 创意搭档</span>
        </div>
        <h2 className="text-ink text-2xl font-extrabold tracking-tight">
          把脑海中的灵感，变成眼前的好图。
        </h2>
        <p className="text-muted mt-1 mb-5 text-xs">一句话，开启你的下一次创作。</p>

        <GenerateForm
          defaultPrompt={readPromptDraft()}
          pending={generate.isPending}
          onSubmit={(input) =>
            generate.mutate(input, {
              onSuccess: (run) => {
                savePromptDraft('')
                navigate(`/candidates/${run.id}`)
              },
            })
          }
        />
        {generate.isError && (
          <p className="text-danger mt-3 text-xs">{errorMessage(generate.error)}</p>
        )}
      </section>

      {/* 独立虚线商品图上传条 */}
      <section className="mt-5">
        <ImageDropzone
          onFile={(file) => upload.mutate(file, { onSuccess: (asset) => openEditor(asset.id) })}
          disabled={upload.isPending || createSession.isPending}
        />
        {upload.isError && (
          <p className="text-danger mt-2 text-xs">{errorMessage(upload.error)}</p>
        )}
      </section>

      {/* 历史素材网格 */}
      <section className="mt-8">
        <div className="mb-4 flex items-end justify-between">
          <div>
            <h2 className="text-ink text-lg font-bold">最近的创意</h2>
            <p className="text-muted mt-0.5 text-xs">历史素材库与会话记录</p>
          </div>
        </div>

        {isPending ? (
          <p className="text-faint text-xs">加载素材库中…</p>
        ) : groups.length === 0 ? (
          <div className="border-line bg-paper rounded-card border p-8 text-center text-xs text-muted">
            还没有素材，先描述画面或上传一张图片。
          </div>
        ) : (
          <AssetLibrary
            groups={groups}
            onOpenSession={(sessionId) => navigate(`/editor/${sessionId}`)}
            onOpenAsset={openEditor}
          />
        )}
      </section>
    </div>
  )
}
