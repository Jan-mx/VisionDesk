const STEPS = [
  {
    num: '01',
    title: '给一个想法',
    desc: '描述画面，或上传你的商品图片。',
  },
  {
    num: '02',
    title: '让创意发生',
    desc: '生成候选，用自然语言打磨每个细节。',
  },
  {
    num: '03',
    title: '把精彩带走',
    desc: '适配多种投放尺寸，一次打包交付。',
  },
]

/** 创作三步指引（复现 creative-v1 方案 A · Steps） */
export default function DeliverySteps() {
  return (
    <section className="border-line/70 mx-auto max-w-[1200px] border-t px-6 pt-12 pb-6">
      <div className="grid grid-cols-1 gap-8 sm:grid-cols-3 sm:gap-10">
        {STEPS.map((step) => (
          <div key={step.num} className="flex items-start gap-4">
            <span className="font-serif text-[34px] italic leading-none text-[#bbb7db] select-none">
              {step.num}
            </span>
            <div>
              <h3 className="text-ink text-[15px] font-bold">{step.title}</h3>
              <p className="text-muted mt-1.5 text-xs leading-relaxed">{step.desc}</p>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
