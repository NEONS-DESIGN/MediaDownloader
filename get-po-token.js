const { generate } = require('youtube-po-token-generator');

async function main() {
  try {
    console.log('トークンを生成中...');
    const result = await generate();
    
    console.log('--- 取得完了 ---');
    console.log('Visitor Data:', result.visitorData);
    console.log('PO Token:', result.poToken);
  } catch (error) {
    console.error('エラー:', error);
  }
}

main();