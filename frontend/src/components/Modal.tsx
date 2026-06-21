import { Modal as AntdModal } from 'antd';

interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
}

export default function Modal({ isOpen, onClose, title, children }: ModalProps) {
  return (
    <AntdModal
      open={isOpen}
      onCancel={onClose}
      title={title}
      footer={null}
      destroyOnClose
      centered
    >
      {children}
    </AntdModal>
  );
}
