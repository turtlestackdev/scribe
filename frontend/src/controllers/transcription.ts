import { Controller } from '@hotwired/stimulus'
import type { TinyMCE } from 'tinymce'

declare let tinymce: TinyMCE

export default class extends Controller {
  declare readonly messageInputTarget: HTMLInputElement
  declare readonly formattedMessageTarget: HTMLInputElement
  declare readonly loadingIconTarget: HTMLElement
  static targets = ['messageInput', 'formattedMessage', 'loadingIcon']

  async connect() {
    const editorId = `#${this.messageInputTarget.id}`
    await tinymce.init({
      selector: editorId,
      menubar: false,
      toolbar: 'undo redo | bold italic | link image',
      statusbar: false,
    })
  }

  publish() {
    const content = tinymce.activeEditor?.getContent()
    if (content) {
      this.formattedMessageTarget.value = content
      this.formattedMessageTarget.form?.requestSubmit()
      this.loadingIconTarget.classList.remove('hidden')
    }
  }
}