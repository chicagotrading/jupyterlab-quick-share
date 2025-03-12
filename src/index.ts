import { ILabShell, JupyterFrontEnd, JupyterFrontEndPlugin } from '@jupyterlab/application';
import { showErrorMessage } from '@jupyterlab/apputils';
import { IFileBrowserFactory } from '@jupyterlab/filebrowser';
import { ISettingRegistry } from '@jupyterlab/settingregistry';
import { shareIcon } from '@jupyterlab/ui-components';
import { requestAPI } from './handler';

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'jupyterlab-quick-share:plugin',
  description: 'Send/receive links that make it easy to share notebooks (and other files)',
  autoStart: true,
  requires: [IFileBrowserFactory, ISettingRegistry],
  activate: async (app: JupyterFrontEnd, factory: IFileBrowserFactory, settingRegistry: ISettingRegistry) => {
    console.log('JupyterLab extension jupyterlab-quick-share is activated!');

    const settings = (await settingRegistry.load(plugin.id)).composite.private as any;
    if (settings.enableJupytextIssue1344Fix) {
      fixJupytextIssue1344(app);
    }

    const { tracker } = factory;
    app.commands.addCommand('jupyterlab-quick-share:share', {
      label: 'Copy Quick Share Link',
      icon: shareIcon,
      isVisible: () => !!tracker.currentWidget && Array.from(tracker.currentWidget.selectedItems()).length === 1,
      execute: async () => {
        const widget = tracker.currentWidget;
        if (!widget) {
          return;
        }
        const selectedFile = widget.selectedItems().next().value.path;
        try {
          const data = await requestAPI<any>(`share?path=${encodeURIComponent(selectedFile)}`);
          await navigator.clipboard.writeText(data.url);
          console.log('Copied to clipboard:', data.url);
        } catch (error) {
          console.error('Failed to copy quick share link:', error);
          showErrorMessage('Quick Share Failed', error as Error);
        }
      }
    });

    app.contextMenu.addItem({
      command: 'jupyterlab-quick-share:share',
      selector: '.jp-DirListing-item[data-isdir="false"]',
      rank: 0
    });
  }
};

function fixJupytextIssue1344(app: JupyterFrontEnd) {
  (app.shell as ILabShell).layoutModified.connect(() => {
    const container = document.querySelector('.jp-Launcher-content');
    if (!container) {
      return;
    }
    const launcherSectionTitles = document.querySelectorAll('.jp-Launcher-sectionTitle');
    const jupytextSection = findLauncherSectionWithTitle('Jupytext', launcherSectionTitles);
    if (!jupytextSection) {
      return;
    }
    container.prepend(jupytextSection);
    findLauncherSectionWithTitle('Notebook', launcherSectionTitles)?.remove();
  });
}

function findLauncherSectionWithTitle(title: string, els: NodeListOf<Element>) {
  return Array.from(els)
    .find(el => el.textContent?.trim() === title)
    ?.closest('.jp-Launcher-section');
}

export default plugin;
